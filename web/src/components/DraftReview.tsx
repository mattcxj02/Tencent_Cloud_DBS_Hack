// A draft that cannot be approved yet: the independent check found a mismatch, was
// unsure, or did not finish. There is deliberately no approve button here.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { DraftBundle, Turn, ValidationIssue, ValidationView } from '../api'
import { describeCode, isConfigurationError } from '../messages'
import { fromDraftLeg } from '../legs'
import type { LegChange, LegView } from '../legs'
import { useAnnounce, useSession } from '../session'
import { LegList } from './LegList'
import { Button, Card, Notice } from './ui'

const FIELD_LABEL: Record<string, string> = {
  action: 'Type of payment',
  recipient: 'Recipient',
  source: 'Account to pay from',
  amount: 'Amount',
  currency: 'Currency',
  asset: 'Stock',
  quantity: 'Number of shares',
  order: 'Order of payments',
  dependency: 'Link between payments',
  missing_leg: 'Missing payment',
  extra_leg: 'Extra payment',
  other: 'Other',
}

/** Python str offsets are code points; slice by code point too. */
function spanText(turns: Turn[], turnId: string, start: number, end: number): string | null {
  const turn = turns.find((t) => t.turn_id === turnId)
  if (!turn) return null
  return Array.from(turn.text).slice(start, end).join('')
}

function IssueItem({ issue, legs, turns }: { issue: ValidationIssue; legs: LegView[]; turns: Turn[] }) {
  const index = legs.findIndex((l) => l.legId === issue.leg_id)
  const quotes = issue.evidence
    .map((e) => spanText(turns, e.turn_id, e.start, e.end))
    .filter((q): q is string => !!q)
  return (
    <li className="rounded-lg border border-amber-600 bg-amber-50 p-3">
      <p className="font-semibold">
        {index >= 0 ? `Payment ${index + 1} · ` : ''}
        {FIELD_LABEL[issue.field] ?? issue.field}
      </p>
      {/* Model-written reason: plain text through React escaping, never HTML. */}
      <p>{issue.reason}</p>
      {issue.supported_value && (
        <p>
          What you said supports: <strong>{issue.supported_value}</strong>
        </p>
      )}
      {quotes.length > 0 && (
        <p className="text-slate-800">
          You said: {quotes.map((q, i) => <q key={i} className="mr-1 italic">{q}</q>)}
        </p>
      )}
    </li>
  )
}

function guidance(v: ValidationView | null, status: DraftBundle['status']): { title: string; body: string; action: 'edit' | 'retry' | 'redraft' | 'restate' | 'wait' | 'unavailable' } {
  if (!v) return { title: 'The independent check result is missing', body: 'This draft cannot be approved without it.', action: 'retry' }
  if (v.code && isConfigurationError(v.code)) return { title: 'The payment assistant is unavailable', body: describeCode(v.code), action: 'unavailable' }
  switch (v.next_action) {
    case 'WAIT':
      return { title: 'Still checking this draft…', body: 'The independent check is running.', action: 'wait' }
    case 'CLARIFY':
      return {
        title: "The independent check wasn't sure this matches what you said",
        body: 'Please say it more precisely, or edit your message. We will prepare a new draft and check it again.',
        action: 'edit',
      }
    case 'RETRY':
      return {
        title: "The independent check didn't finish",
        body: `${describeCode(v.code ?? 'VALIDATOR_UNAVAILABLE')} This draft cannot be approved until the check passes.`,
        action: 'retry',
      }
    case 'RESTATE':
      return { title: 'Please restate your request', body: describeCode('CONTEXT_LIMIT_RESTATE'), action: 'restate' }
    case 'REDRAFT':
    case 'NONE':
      return {
        title: 'This draft is out of date',
        body: 'Something changed since it was checked (for example a balance or your conversation). Prepare a new draft.',
        action: 'redraft',
      }
    default:
      return {
        title: status === 'needs_correction' ? 'This draft needs a correction' : 'This draft cannot be approved',
        body: 'Edit your message, or type the correction (for example “make it 15 dollars”). We will prepare a new draft and check it again.',
        action: 'edit',
      }
  }
}

export function DraftReview({
  draft,
  changes,
  turns,
  onEdit,
  onRedraft,
  onDismiss,
  onNewConversation,
  onValidated,
  onRetryValidation,
}: {
  draft: DraftBundle
  changes: LegChange[]
  turns: Turn[]
  onEdit: () => void
  onRedraft: () => void
  onDismiss: () => void
  onNewConversation: () => void
  onValidated: (v: ValidationView) => void
  onRetryValidation: () => void
}) {
  const { accounts } = useSession()
  const announce = useAnnounce()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const legs = draft.legs.map((l) => fromDraftLeg(l, accounts ?? []))
  const g = guidance(draft.validation, draft.status)
  const [polls, setPolls] = useState(0)

  useEffect(() => {
    headingRef.current?.focus()
    announce(g.title) // once per draft
  }, [draft.bundle_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Validation still running: poll its stored result (read-only) for a short while.
  useEffect(() => {
    if (g.action !== 'wait' || polls >= 15) return
    let active = true
    const controller = new AbortController()
    const id = window.setTimeout(async () => {
      try {
        const v = await api.validation(draft.bundle_id, controller.signal)
        if (!active) return
        if (v.next_action !== 'WAIT') onValidated(v)
        else setPolls((p) => p + 1)
      } catch {
        if (!active) return
        setPolls((p) => p + 1)
      }
    }, 2000)
    return () => { active = false; controller.abort(); window.clearTimeout(id) }
  }, [g.action, polls, draft.bundle_id, onValidated])

  const issues = draft.validation?.issues ?? []
  return (
    <Card title={g.title} titleId="draft-review-heading" headingRef={headingRef}>
      <div className="space-y-4">
        <Notice kind="warning" title="Not ready to approve">
          <p>{g.body}</p>
          {g.action === 'wait' && polls >= 15 && <p>This is taking longer than usual.</p>}
        </Notice>
        {issues.length > 0 && (
          <div>
            <h3 className="mb-2 text-base font-bold">What the independent check found</h3>
            <ul className="space-y-2">
              {issues.map((issue, i) => (
                <IssueItem key={i} issue={issue} legs={legs} turns={turns} />
              ))}
            </ul>
          </div>
        )}
        <div>
          <h3 className="mb-2 text-base font-bold">The draft as prepared</h3>
          <LegList legs={legs} changes={changes} headingLevel={4} />
        </div>
        <div className="flex flex-wrap gap-2">
          {g.action === 'retry' || (g.action === 'wait' && polls >= 15) ? (
            <Button onClick={onRetryValidation}>Retry validation of saved draft</Button>
          ) : g.action === 'redraft' ? (
            <Button onClick={onRedraft}>Prepare a new draft</Button>
          ) : g.action === 'restate' ? (
            <Button onClick={onNewConversation}>Start a new conversation</Button>
          ) : null}
          <Button variant={g.action === 'edit' ? 'primary' : 'secondary'} onClick={onEdit}>
            Edit request
          </Button>
          <Button variant="secondary" onClick={onDismiss}>
            Discard draft
          </Button>
        </div>
      </div>
    </Card>
  )
}
