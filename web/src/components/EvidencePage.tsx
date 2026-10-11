// "You said → We clarified → You approved → Executed": the gateway's record of one
// conversation, with expandable technical details (hashes, validator model/prompt,
// audit chain, trace links). Failed attempts are shown as failed, never as receipts.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, ApiError } from '../api'
import type { EvidenceAuthorization, EvidenceDraft, EvidenceTurn, EvidenceView } from '../api'
import { describeCode, statusLabel } from '../messages'
import { actionLabel } from '../legs'
import { formatMoney } from '../money'
import { formatDateTime, safeHttpUrl, shortHash } from '../format'
import { navigate } from '../router'
import { Badge, Button, Field, Notice, PageTitle, inputClass } from './ui'

function readConv(): string | null {
  try {
    return window.sessionStorage.getItem('dcta.conversation_id')
  } catch {
    return null
  }
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="relative border-l-4 border-slate-400 pb-6 pl-5 last:pb-0">
      <span
        aria-hidden="true"
        className="absolute -left-[14px] top-0 flex h-6 w-6 items-center justify-center rounded-full bg-slate-900 text-sm font-bold text-white"
      >
        {n}
      </span>
      <h2 className="text-lg font-bold">
        <span className="sr-only">Step {n}: </span>
        {title}
      </h2>
      <div className="mt-2 space-y-2">{children}</div>
    </li>
  )
}

function TraceLink({ url }: { url: string | null | undefined }) {
  const safe = safeHttpUrl(url)
  if (!safe) return <span>—</span>
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" className="break-all font-semibold text-indigo-800 underline">
      Open trace<span className="sr-only"> (opens in a new tab)</span> ↗
    </a>
  )
}

function roleText(t: EvidenceTurn): string {
  switch (t.role) {
    case 'user_asr_final':
      return 'You said (voice transcript)'
    case 'user_transcript_edit':
      return 'Your correction'
    case 'user_answer':
      return 'Your answer'
    case 'app_question':
      return 'We asked'
    default:
      return 'You wrote'
  }
}

function TurnLine({ t }: { t: EvidenceTurn }) {
  return (
    <li className="rounded-lg border border-slate-300 bg-white p-2">
      <p className="text-sm font-semibold text-slate-700">
        {roleText(t)} · {formatDateTime(t.created_at)} {t.superseded && <Badge tone="changed">Replaced by a correction</Badge>}
      </p>
      <p className="whitespace-pre-wrap break-words">{t.superseded ? <s>{t.text}</s> : t.text}</p>
    </li>
  )
}

function DraftLine({ d, index }: { d: EvidenceDraft; index: number }) {
  return (
    <li className="rounded-lg border border-slate-300 bg-white p-2">
      <p className="font-semibold">
        Draft {index + 1} · {d.status} · check: {d.validation?.verdict ?? d.validation?.status ?? 'none'}
      </p>
      <ul className="ml-5 list-disc">
        {d.legs.map((l) => (
          <li key={l.leg_id}>
            {actionLabel(l.action)} {formatMoney(l.amount, l.currency)} to {l.display_name}
            {l.masked_account_suffix && <>, ending {l.masked_account_suffix}</>}
          </li>
        ))}
      </ul>
      <details className="mt-1 text-sm">
        <summary className="cursor-pointer font-semibold">Technical details</summary>
        <dl className="mt-1 grid grid-cols-[auto,1fr] gap-x-3">
          <dt className="font-semibold">Bundle</dt>
          <dd className="break-all">{d.bundle_id}</dd>
          <dt className="font-semibold">Bundle hash</dt>
          <dd className="break-all font-mono">{d.bundle_hash}</dd>
          <dt className="font-semibold">Validator model</dt>
          <dd className="break-all">{d.validation?.model ?? '—'}</dd>
          <dt className="font-semibold">Validator prompt hash</dt>
          <dd className="break-all font-mono">{d.validation?.prompt_hash ?? '—'}</dd>
          <dt className="font-semibold">Validation trace</dt>
          <dd>
            <TraceLink url={d.validation?.trace_url} />
          </dd>
        </dl>
        {d.validation && d.validation.issues.length > 0 && (
          <ul className="ml-5 mt-1 list-disc">
            {d.validation.issues.map((i, k) => (
              <li key={k}>
                {i.field}: {i.reason}
              </li>
            ))}
          </ul>
        )}
      </details>
    </li>
  )
}

function AuthorizationLine({ a }: { a: EvidenceAuthorization }) {
  const ok = a.status === 'COMMITTED' && a.receipt
  return (
    <li className="rounded-lg border border-slate-300 bg-white p-2">
      <p className="font-semibold">
        {ok ? <Badge tone="good">✓ {statusLabel(a.status)}</Badge> : <Badge tone={a.status === 'PENDING_APPROVAL' || a.status === 'AWAITING_STEP_UP' ? 'warn' : 'bad'}>{statusLabel(a.status)}</Badge>}
      </p>
      {a.approval ? (
        <p>
          Approved with your passkey at {formatDateTime(a.approval.verified_at)} · user verification (Touch ID/PIN):{' '}
          {a.approval.user_verified ? 'yes' : 'no'}
        </p>
      ) : (
        <p>No passkey approval recorded.</p>
      )}
      {a.step_up && (
        <p>
          Extra one-time code (simulated): {a.step_up.status} · wrong attempts: {a.step_up.attempts}
        </p>
      )}
      {a.status_reason && !ok && <p>Reason: {describeCode(a.status_reason)}</p>}
      <details className="mt-1 text-sm">
        <summary className="cursor-pointer font-semibold">Technical details</summary>
        <dl className="mt-1 grid grid-cols-[auto,1fr] gap-x-3">
          <dt className="font-semibold">Authorization</dt>
          <dd className="break-all">{a.authorization_id}</dd>
          <dt className="font-semibold">Envelope hash</dt>
          <dd className="break-all font-mono">{a.envelope_hash}</dd>
          <dt className="font-semibold">Issued / expires</dt>
          <dd>
            {formatDateTime(a.issued_at)} / {formatDateTime(a.expires_at)}
          </dd>
          {a.approval && (
            <>
              <dt className="font-semibold">Passkey credential</dt>
              <dd className="break-all">{shortHash(a.approval.credential_id, 16)}</dd>
            </>
          )}
        </dl>
      </details>
    </li>
  )
}

function ReceiptLine({ a }: { a: EvidenceAuthorization }) {
  const r = a.receipt
  if (!r) return null
  return (
    <li className="rounded-lg border-2 border-green-700 bg-white p-2">
      <p className="font-semibold">
        Receipt {r.receipt_id} · {formatDateTime(r.committed_at)} · total {formatMoney(r.total_debit)} (simulated)
      </p>
      <ul className="ml-5 list-disc">
        {r.legs.map((l) => (
          <li key={l.leg_id}>
            {actionLabel(l.action)} {formatMoney(l.amount)} to {l.display_name}
            {l.quantity !== undefined && <> ({l.quantity} shares)</>}
            {l.settlement_ref && <> · ref {l.settlement_ref}</>}
          </li>
        ))}
      </ul>
      <details className="mt-1 text-sm">
        <summary className="cursor-pointer font-semibold">Technical details</summary>
        <dl className="mt-1 grid grid-cols-[auto,1fr] gap-x-3">
          <dt className="font-semibold">Payload hash</dt>
          <dd className="break-all font-mono">{r.payload_hash}</dd>
          <dt className="font-semibold">Envelope hash</dt>
          <dd className="break-all font-mono">{r.envelope_hash}</dd>
        </dl>
      </details>
    </li>
  )
}

export function EvidencePage({ conversationId }: { conversationId: string | null }) {
  const id = conversationId ?? readConv()
  const [data, setData] = useState<EvidenceView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [lookup, setLookup] = useState('')

  const load = useCallback(async () => {
    if (!id) return
    setLoading(true)
    setError(null)
    try {
      setData(await api.evidence(id))
    } catch (e) {
      setError(e instanceof ApiError ? describeCode(e.code) : 'Could not load the evidence.')
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  const open = (e: FormEvent) => {
    e.preventDefault()
    if (lookup.trim()) navigate('evidence', lookup.trim())
  }

  const userTurns = data?.turns.filter((t) => t.role !== 'app_question' && t.role !== 'user_answer') ?? []
  const clarifications = data?.turns.filter((t) => t.role === 'app_question' || t.role === 'user_answer') ?? []
  const receipts = data?.authorizations.filter((a) => a.status === 'COMMITTED' && a.receipt) ?? []

  return (
    <div className="space-y-4">
      <PageTitle sub="The bank's own record of this conversation. Model output is shown as plain text.">Evidence trail</PageTitle>

      <form onSubmit={open} className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="flex-1">
          <Field id="evidence-id" label="Conversation ID">
            <input id="evidence-id" className={inputClass} value={lookup} placeholder={id ?? 'conversation ID'} onChange={(e) => setLookup(e.target.value)} />
          </Field>
        </div>
        <Button type="submit" variant="secondary">
          Open
        </Button>
        <Button variant="secondary" onClick={() => void load()} disabled={!id} busy={loading} busyLabel="Loading…">
          Refresh
        </Button>
      </form>

      {!id && <Notice kind="info" title="No conversation yet. Start one on the Chat page." />}
      {error && <Notice kind="error" title={error} focus />}

      {data && (
        <>
          <p className="break-all text-sm text-slate-700">
            Conversation {data.conversation_id} · revision {data.revision}
          </p>
          <ol className="ml-3">
            <Step n={1} title="You said">
              {userTurns.length ? <ul className="space-y-2">{userTurns.map((t) => <TurnLine key={t.turn_id} t={t} />)}</ul> : <p>Nothing yet.</p>}
            </Step>
            <Step n={2} title="We clarified">
              {clarifications.length ? (
                <ul className="space-y-2">{clarifications.map((t) => <TurnLine key={t.turn_id} t={t} />)}</ul>
              ) : (
                <p>No questions were needed.</p>
              )}
              {data.drafts.length > 0 && (
                <>
                  <h3 className="font-semibold">Drafts and independent checks</h3>
                  <ul className="space-y-2">
                    {data.drafts.map((d, i) => (
                      <DraftLine key={d.bundle_id} d={d} index={i} />
                    ))}
                  </ul>
                </>
              )}
            </Step>
            <Step n={3} title="You approved">
              {data.authorizations.length ? (
                <ul className="space-y-2">{data.authorizations.map((a) => <AuthorizationLine key={a.authorization_id} a={a} />)}</ul>
              ) : (
                <p>No approvals requested.</p>
              )}
            </Step>
            <Step n={4} title="Executed">
              {receipts.length ? (
                <ul className="space-y-2">{receipts.map((a) => <ReceiptLine key={a.authorization_id} a={a} />)}</ul>
              ) : (
                <p>Nothing was paid in this conversation.</p>
              )}
            </Step>
          </ol>

          <details className="rounded-xl border border-slate-300 bg-white p-3">
            <summary className="cursor-pointer text-base font-bold">Audit chain and traces</summary>
            <p className="mt-2">
              Audit hash chain:{' '}
              {data.audit.ok ? (
                <Badge tone="good">✓ verified</Badge>
              ) : (
                <Badge tone="bad">✕ broken at event {data.audit.first_bad_seq ?? '?'}</Badge>
              )}{' '}
              <span className="text-sm text-slate-700">(prototype local chain; not regulatory-grade)</span>
            </p>
            {data.trace_urls.length > 0 && (
              <ul className="mt-2 space-y-1">
                {data.trace_urls.map((u, i) => (
                  <li key={i}>
                    <TraceLink url={u} />
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Audit events</caption>
              <thead>
                <tr className="border-b border-slate-400">
                  <th scope="col">#</th>
                  <th scope="col">Time</th>
                  <th scope="col">Source</th>
                  <th scope="col">Step</th>
                </tr>
              </thead>
              <tbody>
                {data.audit.events.map((ev) => (
                  <tr key={ev.seq} className="border-b border-slate-200 align-top">
                    <td className="pr-2 tabular-nums">{ev.seq}</td>
                    <td className="pr-2">{formatDateTime(ev.ts)}</td>
                    <td className="pr-2">{ev.source}</td>
                    <td className="break-all">{ev.step}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </details>
        </>
      )}
    </div>
  )
}
