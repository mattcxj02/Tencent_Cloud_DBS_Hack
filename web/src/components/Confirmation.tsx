// Exact-envelope confirmation. For a draft the gateway marked ready (validated MATCH and
// eligible), this asks the gateway to issue a signing envelope, renders THAT envelope
// (not the draft, not anything a model wrote), and signs the envelope's own WebAuthn
// options. The gateway then verifies, rechecks policy and commits atomically.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { ApproveResult, AuthorizationView, CommittedResult, DraftBundle } from '../api'
import { describeCode, describeCodes, isConfigurationError, splitCode } from '../messages'
import { diffLegs, fromDraftLeg, fromEnvelopeLeg, totalCents } from '../legs'
import type { LegView } from '../legs'
import { formatCents, formatMoney } from '../money'
import { formatSeconds, formatTime, parseIso } from '../format'
import { useCountdown } from '../hooks'
import { PasskeyError, signWithPasskey } from '../passkey'
import { useAnnounce, useSession } from '../session'
import { LegList } from './LegList'
import { Receipt } from './Receipt'
import { StepUp } from './StepUp'
import type { StepUpOutcome } from './StepUp'
import { Badge, Button, Card, Notice } from './ui'

type Phase =
  | { kind: 'issuing' }
  | { kind: 'issue-failed'; code: string }
  | { kind: 'review' }
  | { kind: 'signing' }
  | { kind: 'submitting' }
  | { kind: 'recovering' }
  | { kind: 'step-up'; reasonCodes: string[] }
  | { kind: 'committed'; result: CommittedResult; recovered: boolean }
  | { kind: 'blocked'; codes: string[]; beforeApproval: boolean }
  | { kind: 'expired' }
  | { kind: 'reissue'; code: string }
  | { kind: 'stale'; code: string }
  | { kind: 'cancelled' }
  | { kind: 'unknown' }

const BUSY = new Set(['issuing', 'signing', 'submitting', 'recovering'])

export function Confirmation({
  draft,
  prevLegs,
  onEdit,
  onRedraft,
  onClose,
  onCommitted,
  onFinished,
  existingAuthorizationId,
  onPendingChange,
}: {
  draft: DraftBundle
  existingAuthorizationId?: string
  onPendingChange?: (pending: boolean) => void
  prevLegs: LegView[] | null
  onEdit: () => void
  onRedraft: () => void
  onClose: () => void
  /** A receipt exists: this conversation's requests are done. */
  onCommitted: () => void
  /** "Start a new request" after a receipt. */
  onFinished: () => void
}) {
  const { credentialId, accounts, refreshAccounts } = useSession()
  const announce = useAnnounce()
  const [phase, setPhase] = useState<Phase>({ kind: 'issuing' })
  const [authz, setAuthz] = useState<AuthorizationView | null>(null)
  const [skewMs, setSkewMs] = useState(0)
  const [inlineError, setInlineError] = useState<string | null>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const issuedFor = useRef<string | null>(null)
  const inFlight = useRef(false)
  const warned = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    onPendingChange?.(BUSY.has(phase.kind) || phase.kind === 'unknown' || phase.kind === 'step-up')
  }, [phase.kind, onPendingChange])
  useEffect(() => () => onPendingChange?.(false), [onPendingChange])

  const legs = useMemo(
    () => (authz ? authz.envelope.payload.legs.map((l) => fromEnvelopeLeg(l, accounts ?? [])) : []),
    [authz, accounts],
  )
  // Before an envelope exists (or if issuing failed) the gateway's draft legs give context.
  const draftLegs = useMemo(() => draft.legs.map((l) => fromDraftLeg(l, accounts ?? [])), [draft.legs, accounts])
  const changes = useMemo(() => (prevLegs ? diffLegs(prevLegs, legs) : []), [prevLegs, legs])
  const remaining = useCountdown(
    phase.kind === 'review' || phase.kind === 'signing' || phase.kind === 'submitting' ? authz?.expires_at : null,
    skewMs,
  )

  // ── Issue the signing envelope ───────────────────────────────────
  const issue = useCallback(async () => {
    if (inFlight.current) return
    if (!credentialId) {
      setPhase({ kind: 'issue-failed', code: 'CREDENTIAL_NOT_OWNED' })
      return
    }
    inFlight.current = true
    setInlineError(null)
    setPhase({ kind: 'issuing' })
    try {
      const view = existingAuthorizationId
        ? await api.authorization(existingAuthorizationId)
        : await api.createAuthorization(draft.bundle_id, credentialId)
      if (!mounted.current) return
      const issuedAt = parseIso(view.envelope.issued_at)
      setSkewMs(issuedAt === null ? 0 : issuedAt - Date.now())
      warned.current = false
      setAuthz(view)
      if (view.status === 'COMMITTED') {
        const result = await api.receipt(view.authorization_id)
        if (!mounted.current) return
        setPhase({ kind: 'committed', result, recovered: true })
        onCommitted()
        void refreshAccounts()
        return
      }
      if (view.status === 'AWAITING_STEP_UP') {
        setPhase({ kind: 'step-up', reasonCodes: view.reason_codes })
        return
      }
      if (view.status !== 'PENDING_APPROVAL') {
        setPhase(view.status === 'CANCELLED' ? { kind: 'cancelled' } : { kind: 'stale', code: `AUTHORIZATION_${view.status}` })
        return
      }
      const conversation = await api.conversation(view.envelope.payload.conversation_id)
      if (!mounted.current) return
      if (conversation.revision !== view.envelope.payload.conversation_revision) {
        setPhase({ kind: 'stale', code: 'AUTHORIZATION_SUPERSEDED' })
        return
      }
      if (existingAuthorizationId) {
        const validation = await api.validation(view.envelope.payload.bundle_id)
        if (!mounted.current) return
        if (!validation.eligible_for_confirmation || validation.next_action !== 'CONFIRM') {
          setPhase({ kind: 'stale', code: 'SEMANTIC_VALIDATION_REQUIRED' })
          return
        }
      }
      setPhase({ kind: 'review' })
      announce(
        `Confirmation ready: ${view.envelope.payload.legs.length} payments. Review every line, then approve within 2 minutes.`,
      )
    } catch (e) {
      if (!mounted.current) return
      const code = e instanceof ApiError ? e.code : 'NETWORK_ERROR'
      const base = splitCode(code).base
      if (base === 'POLICY_BLOCK' || base === 'NO_ACTIVE_MANDATE') {
        const codes = e instanceof ApiError && e.reasonCodes.length ? e.reasonCodes : [base]
        setPhase({ kind: 'blocked', codes, beforeApproval: true })
      } else if (['DRAFT_STALE_REVISION', 'DRAFT_NOT_ELIGIBLE', 'QUOTE_EXPIRED', 'SEMANTIC_VALIDATION_REQUIRED'].includes(base)) {
        setPhase({ kind: 'stale', code })
      } else {
        setPhase({ kind: 'issue-failed', code })
      }
    } finally {
      inFlight.current = false
    }
  }, [credentialId, draft.bundle_id, announce, existingAuthorizationId, onCommitted, refreshAccounts])

  useEffect(() => {
    // Issue once per draft (a ref, so React StrictMode's double effect cannot issue twice).
    if (issuedFor.current === draft.bundle_id) return
    issuedFor.current = draft.bundle_id
    void issue()
  }, [draft.bundle_id, issue])

  // Focus the heading whenever the screen's meaning changes. Returning to review after a
  // failed signature keeps focus on the error message instead.
  const prevKind = useRef<Phase['kind']>('issuing')
  useEffect(() => {
    const from = prevKind.current
    prevKind.current = phase.kind
    const fresh = phase.kind === 'review' && from === 'issuing'
    if (fresh || ['blocked', 'expired', 'reissue', 'stale', 'cancelled', 'unknown', 'issue-failed'].includes(phase.kind)) {
      headingRef.current?.focus()
    }
  }, [phase.kind])

  // Expiry: warn once at 30 s, then close the approval.
  useEffect(() => {
    if (remaining === null) return
    if (remaining <= 30 && remaining > 0 && !warned.current) {
      warned.current = true
      announce('Less than 30 seconds left to approve.')
    }
    if (remaining === 0 && phase.kind === 'review') {
      setPhase({ kind: 'expired' })
      announce('The approval window closed. Nothing was paid.')
    }
  }, [remaining, phase.kind, announce])

  // ── Outcomes ─────────────────────────────────────────────────────
  const committed = useCallback(
    (result: CommittedResult, recovered: boolean) => {
      if (!mounted.current) return
      setPhase({ kind: 'committed', result, recovered })
      onCommitted()
      announce(`Paid. Total debit ${formatMoney(result.receipt.total_debit)}. Simulated bank.`)
      void refreshAccounts()
      window.dispatchEvent(new CustomEvent('dcta:task-complete'))
    },
    [announce, refreshAccounts, onCommitted],
  )

  const applyResult = useCallback(
    (r: ApproveResult) => {
      if (!mounted.current) return
      if (r.status === 'COMMITTED') committed(r, false)
      else if (r.status === 'AWAITING_STEP_UP') setPhase({ kind: 'step-up', reasonCodes: r.reason_codes ?? [] })
      else if (r.status === 'BLOCKED') {
        setPhase({ kind: 'blocked', codes: r.reason_codes ?? [], beforeApproval: false })
        announce('Blocked. Nothing was paid.')
      } else setPhase({ kind: 'cancelled' })
    },
    [committed, announce],
  )

  /** Lost or ambiguous response: read the stored state. Never assume success or failure. */
  const recover = useCallback(async () => {
    if (!authz) return
    setPhase({ kind: 'recovering' })
    announce('Checking whether your payment went through…')
    try {
      committed(await api.receipt(authz.authorization_id), true)
      return
    } catch (e) {
      if (!(e instanceof ApiError) || e.code !== 'NOT_COMMITTED') {
        setPhase({ kind: 'unknown' })
        return
      }
      if (!mounted.current) return
      const status = typeof e.body?.status === 'string' ? e.body.status : ''
      if (status === 'AWAITING_STEP_UP') setPhase({ kind: 'step-up', reasonCodes: e.reasonCodes })
      else if (status === 'BLOCKED') setPhase({ kind: 'blocked', codes: e.reasonCodes, beforeApproval: false })
      else if (status === 'EXPIRED') setPhase({ kind: 'expired' })
      else if (status === 'SUPERSEDED') setPhase({ kind: 'stale', code: 'AUTHORIZATION_SUPERSEDED' })
      else if (status === 'CANCELLED') setPhase({ kind: 'cancelled' })
      else if (status === 'PENDING_APPROVAL') {
        setPhase({ kind: 'review' })
        setInlineError('The bank currently shows this approval as pending. Any retry uses this same approval; check its result again if you are unsure.')
      } else setPhase({ kind: 'unknown' })
    }
  }, [authz, announce, committed])

  const approve = async () => {
    if (!authz || inFlight.current) return
    if (remaining === 0) {
      setPhase({ kind: 'expired' })
      return
    }
    inFlight.current = true
    setInlineError(null)
    setPhase({ kind: 'signing' })
    announce('Waiting for your passkey (Touch ID, Windows Hello or security key).')
    try {
      let assertion
      try {
        // Sign the challenge the gateway derived from THIS stored envelope.
        assertion = await signWithPasskey(authz.webauthn_options)
      } catch (e) {
        setPhase({ kind: 'review' })
        setInlineError(e instanceof PasskeyError ? e.message : 'The passkey could not sign. Nothing was approved.')
        return
      }
      if (!mounted.current) return
      setPhase({ kind: 'submitting' })
      try {
        applyResult(await api.approve(authz.authorization_id, authz.envelope_hash, assertion))
      } catch (e) {
        if (!(e instanceof ApiError) || e.network) {
          await recover()
          return
        }
        const base = splitCode(e.code).base
        if (base === 'AUTHORIZATION_EXPIRED') setPhase({ kind: 'expired' })
        else if (base === 'AUTHORIZATION_SUPERSEDED') setPhase({ kind: 'stale', code: base })
        else if (base === 'POLICY_BLOCK') setPhase({ kind: 'blocked', codes: e.reasonCodes.length ? e.reasonCodes : [base], beforeApproval: false })
        else if (base === 'NOT_PENDING' || base === 'IDEMPOTENCY_CONFLICT') await recover()
        else if (base === 'ALREADY_APPROVED' || base === 'COMMIT_FAILED' || base === 'COMMIT_NOT_IMPLEMENTED')
          await recover()
        else if (base === 'ENVELOPE_HASH_MISMATCH') {
          setAuthz(await api.authorization(authz.authorization_id))
          setPhase({ kind: 'review' })
          setInlineError(describeCode(base))
        } else {
          setPhase({ kind: 'review' })
          setInlineError(describeCode(e.code))
        }
      }
    } catch {
      setPhase({ kind: 'unknown' })
    } finally {
      inFlight.current = false
    }
  }

  const cancel = async () => {
    if (inFlight.current) return
    if (!authz || (phase.kind !== 'review' && phase.kind !== 'step-up')) {
      onClose() // nothing pending at the bank: just dismiss
      return
    }
    inFlight.current = true
    let lost = false
    try {
      await api.cancelAuthorization(authz.authorization_id)
      setPhase({ kind: 'cancelled' })
      announce('Cancelled. Nothing was paid.')
    } catch (e) {
      if (e instanceof ApiError && !e.network && splitCode(e.code).base !== 'NOT_PENDING') {
        setInlineError(describeCode(e.code))
      } else {
        lost = true
      }
    } finally {
      inFlight.current = false
    }
    if (lost) await recover()
  }

  const onStepUp = useCallback(
    (o: StepUpOutcome) => {
      if (o.result) applyResult(o.result)
      else if (o.expired) setPhase({ kind: 'expired' })
      else if (o.cancelled) {
        setPhase({ kind: 'cancelled' })
        announce('Cancelled. Nothing was paid.')
      } else if (o.lost) void recover()
    },
    [applyResult, recover, announce],
  )

  // ── Render ───────────────────────────────────────────────────────
  if (phase.kind === 'committed') {
    return (
      <Receipt
        result={phase.result}
        recovered={phase.recovered}
        accounts={accounts ?? []}
        conversationId={authz?.envelope.payload.conversation_id ?? null}
        onNew={onFinished}
      />
    )
  }
  if (phase.kind === 'step-up' && authz) {
    return <StepUp authz={authz} reasonCodes={phase.reasonCodes} skewMs={skewMs} onOutcome={onStepUp} />
  }

  const busy = BUSY.has(phase.kind)
  const canLeave = !(existingAuthorizationId && ['issue-failed', 'issuing', 'recovering', 'unknown'].includes(phase.kind))
  const validation = draft.validation
  const title = headingFor(phase, legs.length || draft.legs.length)
  const total = totalCents(legs)

  return (
    <Card title={title} titleId="confirm-heading" headingRef={headingRef}>
      <div className="space-y-4">
        {phase.kind === 'issuing' && <Notice kind="info" title="Preparing the exact details for you to approve…" />}
        {phase.kind === 'recovering' && <Notice kind="info" title="Checking whether your payment went through…" />}

        {phase.kind === 'blocked' && (
          <Notice kind="error" title={phase.beforeApproval ? 'This cannot go ahead' : 'Blocked — nothing was paid'}>
            <ul className="ml-5 list-disc">
              {describeCodes(phase.codes.length ? phase.codes : ['POLICY_BLOCK']).map((r) => (
                <li key={r.code}>
                  {r.message} <span className="text-sm">({r.code})</span>
                </li>
              ))}
            </ul>
            <p>These are bank rules. The assistant and the independent check cannot override them.</p>
          </Notice>
        )}
        {phase.kind === 'expired' && (
          <Notice kind="warning" title="The 2-minute approval window closed">
            <p>Nothing was paid. Get fresh details to approve again.</p>
          </Notice>
        )}
        {phase.kind === 'reissue' && (
          <Notice kind="warning" title={describeCode(phase.code)}>
            <p>Nothing was paid. Get fresh details to try again.</p>
          </Notice>
        )}
        {phase.kind === 'stale' && (
          <Notice kind="warning" title="These details are out of date">
            <p>{describeCode(phase.code)}</p>
            <p>Nothing was paid. We will prepare a new draft from your latest conversation.</p>
          </Notice>
        )}
        {phase.kind === 'issue-failed' && (
          <Notice kind="error" title="We couldn't prepare the approval">
            <p>{describeCode(phase.code)}</p>
          </Notice>
        )}
        {phase.kind === 'cancelled' && <Notice kind="success" title="Cancelled. Nothing was paid." />}
        {phase.kind === 'unknown' && (
          <Notice kind="error" title="We couldn't confirm the result yet">
            <p>Please check again. The bank never pays the same approval twice.</p>
          </Notice>
        )}

        {authz && legs.length > 0 && !['cancelled', 'stale'].includes(phase.kind) && (
          <>
            <VerdictLine verdict={validation?.verdict ?? null} />
            <LegList legs={legs} changes={changes} />
          </>
        )}
        {!authz && (phase.kind === 'blocked' || phase.kind === 'issue-failed') && (
          <div>
            <p className="mb-2 font-semibold">Draft details (not approved):</p>
            <LegList legs={draftLegs} />
          </div>
        )}

        {(phase.kind === 'review' || phase.kind === 'signing' || phase.kind === 'submitting') && (
          <p className="text-base" role="timer" aria-live="off">
            Approve within{' '}
            <strong className="tabular-nums">{remaining === null ? '—' : formatSeconds(remaining)}</strong>
            {remaining !== null && remaining <= 30 && (
              <>
                {' '}
                <Badge tone="warn">Less than 30 seconds left</Badge>
              </>
            )}
            {authz && <span className="text-sm text-slate-700"> (until {formatTime(authz.expires_at)})</span>}
          </p>
        )}

        {inlineError && <Notice kind="error" title={inlineError} focus />}

        <div className="flex flex-wrap gap-2">
          {(phase.kind === 'review' || phase.kind === 'signing' || phase.kind === 'submitting') && (
            <Button
              variant="approve"
              onClick={approve}
              busy={phase.kind !== 'review'}
              busyLabel={phase.kind === 'signing' ? 'Waiting for Touch ID / Windows Hello…' : 'Sending approval…'}
              disabled={busy || remaining === 0}
            >
              {total === null ? 'Approve with passkey' : `Approve SGD ${formatCents(total)} with passkey`}
            </Button>
          )}
          {(phase.kind === 'expired' || phase.kind === 'reissue' || (phase.kind === 'issue-failed' && !isConfigurationError(phase.code))) && (
            <Button onClick={() => void issue()}>{existingAuthorizationId ? 'Check existing approval' : 'Get fresh details'}</Button>
          )}
          {phase.kind === 'stale' && <Button onClick={onRedraft}>Prepare a new draft</Button>}
          {phase.kind === 'unknown' && <Button onClick={() => void recover()}>Check again</Button>}
          {canLeave && !['cancelled', 'unknown', 'recovering'].includes(phase.kind) && (
            <Button variant="secondary" onClick={onEdit} disabled={busy}>
              Edit request
            </Button>
          )}
          {phase.kind === 'cancelled' || phase.kind === 'blocked' ? (
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
          ) : (
            canLeave && phase.kind !== 'unknown' && (
              <Button variant="danger" onClick={() => void cancel()} disabled={busy}>
                Cancel
              </Button>
            )
          )}
        </div>

        {authz && <EnvelopeDetails authz={authz} />}
      </div>
    </Card>
  )
}

function headingFor(phase: Phase, count: number): string {
  const what = count === 1 ? 'payment' : `${count} payments`
  switch (phase.kind) {
    case 'blocked':
      return 'Payment blocked'
    case 'expired':
      return 'Approval window closed'
    case 'cancelled':
      return 'Cancelled'
    case 'stale':
      return 'Draft out of date'
    case 'unknown':
      return 'Result not confirmed yet'
    case 'issue-failed':
      return 'Approval not ready'
    default:
      return count === 1 ? 'Confirm this payment' : `Confirm these ${what}`
  }
}

function VerdictLine({ verdict }: { verdict: string | null }) {
  if (verdict === 'MATCH') {
    return (
      <p className="text-base">
        <Badge tone="good">✓ Independent check: matches what you said</Badge>{' '}
        <span className="text-sm text-slate-700">An automated second reading. Please still check every line.</span>
      </p>
    )
  }
  return (
    <p className="text-base">
      <Badge tone="warn">Independent check: {verdict ?? 'not available'}</Badge>
    </p>
  )
}

function EnvelopeDetails({ authz }: { authz: AuthorizationView }) {
  const env = authz.envelope
  return (
    <details className="rounded-lg border border-slate-300 p-3 text-sm">
      <summary className="cursor-pointer font-semibold">Technical details: what your passkey signs</summary>
      <dl className="mt-2 grid grid-cols-[auto,1fr] gap-x-3 gap-y-1">
        <dt className="font-semibold">Operation</dt>
        <dd>{env.operation}</dd>
        <dt className="font-semibold">Authorization</dt>
        <dd className="break-all">{authz.authorization_id}</dd>
        <dt className="font-semibold">Envelope hash</dt>
        <dd className="break-all font-mono">{authz.envelope_hash}</dd>
        <dt className="font-semibold">Draft (bundle)</dt>
        <dd className="break-all">{env.operation_id}</dd>
        <dt className="font-semibold">Conversation revision</dt>
        <dd>{env.conversation_revision ?? '—'}</dd>
        <dt className="font-semibold">Mandate version</dt>
        <dd>{env.active_mandate_version}</dd>
        <dt className="font-semibold">Issued / expires</dt>
        <dd>
          {formatTime(env.issued_at)} / {formatTime(env.expires_at)}
        </dd>
        <dt className="font-semibold">Nonce</dt>
        <dd className="break-all font-mono">{env.nonce}</dd>
      </dl>
      <p className="mt-2">
        The passkey signs a SHA-256 hash of these exact stored bytes. The bank rechecks every rule before paying.
      </p>
    </details>
  )
}
