// Mandate settings: what the assistant may pay. Decreases and pause apply at once;
// increases and resume wait for a cooling period (old settings stay active meanwhile).
// Every change is a signed update_mandate envelope, approved with the passkey.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from '../api'
import type { MandateChangeRequest, MandateChangeView, MandateSettings, MandateView } from '../api'
import { describeCode, splitCode } from '../messages'
import { ACTION_LABEL, actionLabel } from '../legs'
import { formatMoney, isPositiveMoney, toCents } from '../money'
import { formatDateTime, formatDuration, formatSeconds, parseIso } from '../format'
import { useCountdown } from '../hooks'
import { PasskeyError, signWithPasskey } from '../passkey'
import { useAnnounce, useSession } from '../session'
import { Badge, Button, Card, Field, Notice, PageTitle, inputClass } from './ui'

const ACTIONS = Object.keys(ACTION_LABEL)

type Direction = 'tighten' | 'loosen' | 'mixed' | 'none' | 'invalid'

function classify(cur: MandateSettings, perLeg: string, daily: string, actions: string[]): Direction {
  if (!isPositiveMoney(perLeg) || !isPositiveMoney(daily)) return 'invalid'
  let tighten = false
  let loosen = false
  const cmp = (now: string, next: string) => {
    const d = toCents(next) - toCents(now)
    if (d < 0) tighten = true
    if (d > 0) loosen = true
  }
  cmp(cur.per_leg_limit, perLeg)
  cmp(cur.daily_limit, daily)
  if (cur.allowed_actions.some((a) => !actions.includes(a))) tighten = true
  if (actions.some((a) => !cur.allowed_actions.includes(a))) loosen = true
  if (tighten && loosen) return 'mixed'
  return tighten ? 'tighten' : loosen ? 'loosen' : 'none'
}

function actionsText(actions: string[]): string {
  return actions.length ? actions.map(actionLabel).join(', ') : 'none'
}

function SettingsTable({ before, after }: { before: MandateSettings; after?: MandateSettings | null }) {
  const rows: [string, string, string | null][] = [
    ['Per-payment limit', formatMoney(before.per_leg_limit), after ? formatMoney(after.per_leg_limit) : null],
    ['Daily limit', formatMoney(before.daily_limit), after ? formatMoney(after.daily_limit) : null],
    ['Allowed payments', actionsText(before.allowed_actions), after ? actionsText(after.allowed_actions) : null],
    ['Payments through assistant', before.paused ? 'Paused' : 'On', after ? (after.paused ? 'Paused' : 'On') : null],
  ]
  return (
    <table className="w-full text-left text-base">
      <thead>
        <tr className="border-b border-slate-400">
          <th scope="col" className="py-1 pr-2">Setting</th>
          <th scope="col" className="py-1 pr-2">{after ? 'Now' : 'Value'}</th>
          {after && <th scope="col" className="py-1">After change</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, now, next]) => (
          <tr key={label} className="border-b border-slate-200 align-top">
            <th scope="row" className="py-1 pr-2 font-semibold">{label}</th>
            <td className="py-1 pr-2">{now}</td>
            {after && (
              <td className="py-1">
                {next !== now ? (
                  <>
                    <strong>{next}</strong> <Badge tone="changed">Changed</Badge>
                  </>
                ) : (
                  <span>{next} (no change)</span>
                )}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ── Confirm a requested change with the passkey ──────────────────────

function ConfirmChange({
  view,
  onDone,
  onCancel,
}: {
  view: MandateChangeView
  onDone: (message: string) => void
  onCancel: () => void
}) {
  const announce = useAnnounce()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [busy, setBusy] = useState<null | 'sign' | 'send' | 'cancel'>(null)
  const [error, setError] = useState<string | null>(null)
  const [expired, setExpired] = useState(false)
  const [skew] = useState(() => (parseIso(view.envelope.issued_at) ?? Date.now()) - Date.now())
  const remaining = useCountdown(view.expires_at, skew)
  const inFlight = useRef(false)
  const immediate = view.change.applies === 'immediately'

  useEffect(() => {
    headingRef.current?.focus()
    announce('Review the mandate change, then approve with your passkey within 2 minutes.')
  }, [announce])

  useEffect(() => {
    if (remaining === 0 && busy === null) setExpired(true)
  }, [remaining, busy])

  const approve = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setError(null)
    try {
      setBusy('sign')
      let assertion
      try {
        assertion = await signWithPasskey(view.webauthn_options)
      } catch (e) {
        setError(e instanceof PasskeyError ? e.message : 'The passkey could not sign. Nothing changed.')
        return
      }
      setBusy('send')
      try {
        const res = await api.approveMandateChange(view.authorization_id, view.envelope_hash, assertion)
        if (res.status !== 'COMMITTED') {
          setError(describeCode(res.reason_codes?.[0] ?? res.status))
          return
        }
        const outcome = res.receipt?.outcome
        onDone(
          outcome === 'PENDING_COOLING'
            ? `Change approved. It takes effect at ${formatDateTime(res.receipt?.effective_at)} after the cooling period.`
            : 'Change approved and applied now.',
        )
      } catch (e) {
        if (e instanceof ApiError && !e.network) {
          const base = splitCode(e.code).base
          if (base === 'AUTHORIZATION_EXPIRED') setExpired(true)
          else setError(describeCode(e.code))
          return
        }
        // Lost response: read the stored state of this same authorization.
        try {
          const now = await api.mandateChange(view.authorization_id)
          if (now.status === 'COMMITTED') onDone('Change approved (confirmed after a connection problem).')
          else setError(`The approval did not complete (status: ${now.status}). Nothing changed.`)
        } catch {
          setError("We couldn't confirm the result. Reload this page to see your current settings.")
        }
      }
    } finally {
      inFlight.current = false
      setBusy(null)
    }
  }

  const cancel = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy('cancel')
    try {
      await api.cancelAuthorization(view.authorization_id)
    } catch {
      // Expired or already closed: either way nothing changes.
    } finally {
      inFlight.current = false
      setBusy(null)
      onCancel()
    }
  }

  const requested = {
    per_leg_limit: view.envelope.payload.per_leg_limit ?? view.change.current?.per_leg_limit ?? '',
    daily_limit: view.envelope.payload.daily_limit ?? view.change.current?.daily_limit ?? '',
    allowed_actions: view.envelope.payload.allowed_actions,
    paused: view.envelope.payload.paused,
  }

  return (
    <Card title="Confirm mandate change" titleId="mandate-confirm-heading" headingRef={headingRef}>
      <div className="space-y-3">
        {view.change.current && <SettingsTable before={view.change.current} after={requested} />}
        <Notice kind={immediate ? 'info' : 'warning'} title={immediate ? 'Applies immediately' : `Takes effect after a ${view.change.cooling_hours}-hour cooling period`}>
          {immediate ? (
            <p>Decreases and pausing take effect as soon as you approve. Any pending increase is cancelled.</p>
          ) : (
            <p>
              Your current settings stay in force until then. You can cancel it any time by making a decrease or pausing.
            </p>
          )}
        </Notice>
        <p className="text-sm text-slate-800">{view.scope_note}</p>
        {expired ? (
          <Notice kind="warning" title="The 2-minute approval window closed. Nothing changed.">
            <Button onClick={onCancel}>Back to settings</Button>
          </Notice>
        ) : (
          <>
            <p role="timer" aria-live="off">
              Approve within <strong className="tabular-nums">{remaining === null ? '—' : formatSeconds(remaining)}</strong>
            </p>
            {error && <Notice kind="error" title={error} focus />}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="approve"
                onClick={approve}
                busy={busy === 'sign' || busy === 'send'}
                busyLabel={busy === 'sign' ? 'Waiting for your passkey…' : 'Sending approval…'}
                disabled={busy !== null}
              >
                Approve with passkey (Touch ID / Windows Hello)
              </Button>
              <Button variant="danger" onClick={cancel} disabled={busy !== null}>
                Cancel
              </Button>
            </div>
          </>
        )}
        <details className="text-sm">
          <summary className="cursor-pointer font-semibold">Technical details: what your passkey signs</summary>
          <dl className="mt-2 grid grid-cols-[auto,1fr] gap-x-3">
            <dt className="font-semibold">Operation</dt>
            <dd>{view.envelope.operation}</dd>
            <dt className="font-semibold">Envelope hash</dt>
            <dd className="break-all font-mono">{view.envelope_hash}</dd>
            <dt className="font-semibold">Based on mandate version</dt>
            <dd>{view.envelope.active_mandate_version}</dd>
          </dl>
        </details>
      </div>
    </Card>
  )
}

// ── Page ─────────────────────────────────────────────────────────────

export function MandatePage() {
  const { credentialId } = useSession()
  const announce = useAnnounce()
  const [view, setView] = useState<MandateView | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [perLeg, setPerLeg] = useState('')
  const [daily, setDaily] = useState('')
  const [actions, setActions] = useState<string[]>([])
  const [pending, setPending] = useState<MandateChangeView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [nowMs, setNowMs] = useState(Date.now())

  const load = useCallback(async () => {
    try {
      const v = await api.mandate()
      setView(v)
      setPerLeg(v.current.per_leg_limit)
      setDaily(v.current.daily_limit)
      setActions(v.current.allowed_actions)
      setLoadError(null)
    } catch (e) {
      setLoadError(e instanceof ApiError ? describeCode(e.code) : 'Could not load your mandate.')
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => setNowMs(Date.now()), 30_000)
    return () => window.clearInterval(id)
  }, [load])

  const request = async (body: Omit<MandateChangeRequest, 'credential_id'>) => {
    if (busy) return
    if (!credentialId) {
      setError(describeCode('CREDENTIAL_NOT_OWNED'))
      return
    }
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      setPending(await api.requestMandateChange({ credential_id: credentialId, ...body }))
    } catch (e) {
      setError(e instanceof ApiError ? describeCode(e.code) : 'Could not prepare the change.')
    } finally {
      setBusy(false)
    }
  }

  if (loadError) return <><PageTitle>Mandate settings</PageTitle><Notice kind="error" title={loadError} focus /></>
  if (!view) return <><PageTitle>Mandate settings</PageTitle><p>Loading…</p></>

  const cur = view.current
  const direction = classify(cur, perLeg, daily, actions)

  const submitLimits = (e: FormEvent) => {
    e.preventDefault()
    if (direction === 'none' || direction === 'mixed' || direction === 'invalid') return
    const body: Omit<MandateChangeRequest, 'credential_id'> = {}
    if (toCents(perLeg) !== toCents(cur.per_leg_limit)) body.per_leg_limit = perLeg.trim()
    if (toCents(daily) !== toCents(cur.daily_limit)) body.daily_limit = daily.trim()
    const sameActions =
      actions.length === cur.allowed_actions.length && actions.every((a) => cur.allowed_actions.includes(a))
    if (!sameActions) body.allowed_actions = ACTIONS.filter((a) => actions.includes(a))
    void request(body)
  }

  const onDone = (message: string) => {
    setPending(null)
    setDone(message)
    announce(message)
    void load()
  }

  const cooling = view.cooling_hours
  const pendingLeft = view.pending?.effective_at ? Math.max(0, ((parseIso(view.pending.effective_at) ?? nowMs) - nowMs) / 1000) : null

  return (
    <div className="space-y-4">
      <PageTitle sub="What this assistant may pay on your behalf (simulated bank).">Mandate settings</PageTitle>

      {done && <Notice kind="success" title={done} focus />}

      {pending ? (
        <ConfirmChange view={pending} onDone={onDone} onCancel={() => setPending(null)} />
      ) : (
        <>
          <Card
            title="Current settings"
            titleId="mandate-current"
            actions={<Badge tone={cur.paused ? 'warn' : cur.expired ? 'bad' : 'good'}>{cur.paused ? 'Paused' : cur.expired ? 'Expired' : 'Active'}</Badge>}
          >
            <SettingsTable before={cur} />
            <p className="mt-2 text-sm text-slate-700">
              Version {cur.version}, in force since {formatDateTime(cur.effective_at)} · up to {cur.max_legs} payments per request
              {cur.expires_at && <> · expires {formatDateTime(cur.expires_at)}</>}
            </p>
          </Card>

          {view.pending && (
            <Card title="Pending change (cooling period)" titleId="mandate-pending">
              <SettingsTable before={cur} after={view.pending} />
              <p className="mt-2 text-base">
                Takes effect at <strong>{formatDateTime(view.pending.effective_at)}</strong>
                {pendingLeft !== null && <> (in {formatDuration(Math.round(pendingLeft))})</>}. Until then the current settings
                above apply. A decrease or pause cancels this pending increase.
              </p>
            </Card>
          )}

          <Card title={cur.paused ? 'Resume payments' : 'Pause payments'} titleId="mandate-pause">
            <p className="mb-2 text-base">
              {cur.paused
                ? `Resuming is an increase, so it takes effect after a ${cooling}-hour cooling period.`
                : 'Pausing takes effect immediately and cancels any pending increase.'}
            </p>
            <p className="mb-3 text-base font-semibold">{view.scope_note}</p>
            <Button variant={cur.paused ? 'primary' : 'danger'} onClick={() => request({ paused: !cur.paused })} busy={busy} busyLabel="Preparing…">
              {cur.paused ? 'Resume payments through the assistant' : 'Pause payments through the assistant'}
            </Button>
          </Card>

          <Card title="Change limits or payment types" titleId="mandate-limits">
            <form onSubmit={submitLimits} className="space-y-3" noValidate>
              <p className="text-base">
                One direction per request: lower limits / remove types (applies now), <em>or</em> raise limits / add types
                (after {cooling} hours).
              </p>
              <Field id="per-leg" label="Per-payment limit (SGD)">
                <input id="per-leg" className={`${inputClass} max-w-[14rem]`} inputMode="decimal" value={perLeg} onChange={(e) => setPerLeg(e.target.value)} />
              </Field>
              <Field id="daily" label="Daily limit (SGD)">
                <input id="daily" className={`${inputClass} max-w-[14rem]`} inputMode="decimal" value={daily} onChange={(e) => setDaily(e.target.value)} />
              </Field>
              <fieldset>
                <legend className="text-base font-semibold">Payment types the assistant may make</legend>
                <div className="mt-1 grid gap-1 sm:grid-cols-2">
                  {ACTIONS.map((a) => (
                    <label key={a} className="flex min-h-[44px] items-center gap-2 text-base">
                      <input
                        type="checkbox"
                        className="h-5 w-5"
                        checked={actions.includes(a)}
                        onChange={(e) => setActions((prev) => (e.target.checked ? [...prev, a] : prev.filter((x) => x !== a)))}
                      />
                      {actionLabel(a)}
                    </label>
                  ))}
                </div>
              </fieldset>
              <p aria-live="polite" className="text-base">
                {direction === 'tighten' && <Badge tone="good">Decrease: applies immediately</Badge>}
                {direction === 'loosen' && <Badge tone="warn">Increase: takes effect after {cooling} hours</Badge>}
                {direction === 'mixed' && <Badge tone="bad">Mixed: split into a decrease and a separate increase</Badge>}
                {direction === 'invalid' && <Badge tone="bad">Enter positive amounts with at most 2 decimals</Badge>}
                {direction === 'none' && <span className="text-slate-700">No changes yet.</span>}
              </p>
              {error && <Notice kind="error" title={error} focus />}
              <Button type="submit" busy={busy} busyLabel="Preparing…" disabled={direction === 'none' || direction === 'mixed' || direction === 'invalid'}>
                Review change
              </Button>
            </form>
          </Card>

          {view.history.length > 0 && (
            <Card title="Recent changes" titleId="mandate-history">
              <ul className="space-y-1 text-base">
                {view.history.map((h) => (
                  <li key={h.change_id}>
                    {formatDateTime(h.requested_at)} · {h.direction === 'tighten' ? 'Decrease/pause' : 'Increase/resume'} ·{' '}
                    <strong>{h.status}</strong>
                    {h.status_reason && <> ({describeCode(h.status_reason)})</>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
