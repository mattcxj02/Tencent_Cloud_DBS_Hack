// Renders payment legs exactly as given (gateway draft or stored envelope fields).
// All text is rendered through React escaping; nothing here interprets HTML.
import type { LegChange, LegView } from '../legs'
import { actionLabel, totalCents } from '../legs'
import { formatCents, formatMoney } from '../money'
import { formatTime } from '../format'
import { Badge } from './ui'

const TARGET_LABEL: Record<string, string> = {
  transfer: 'To',
  pay_bill: 'Biller',
  pay_card: 'Card',
  buy_equity: 'Stock',
}

function EquityDetails({ leg }: { leg: LegView }) {
  const e = leg.equity
  if (!e) return null
  return (
    <div className="mt-2 rounded-lg border border-slate-300 bg-slate-50 p-3 text-base">
      <p>
        <strong>
          {e.quantity} {e.quantity === 1 ? 'share' : 'shares'}
        </strong>{' '}
        at {formatMoney(e.unit_price)} each = <strong>{formatMoney(e.actual_debit)}</strong>
      </p>
      <p>
        Budget {formatMoney(e.budget)} · <strong>{formatMoney(e.residual_cash)}</strong> stays in your account
      </p>
      <p className="text-sm text-slate-700">
        Whole shares only: the leftover part of your budget is not spent.
        {e.quote_expires_at && <> Price fixed until {formatTime(e.quote_expires_at)} (simulated SGX quote).</>}
      </p>
    </div>
  )
}

function ChangeNote({ change }: { change: LegChange }) {
  if (change.kind === 'added') return <Badge tone="changed">New in this version</Badge>
  return (
    <div className="mt-2 rounded-lg border-l-4 border-indigo-700 bg-indigo-50 p-2 text-base text-indigo-950">
      <p className="font-semibold">Changed since the last draft:</p>
      <ul className="list-none">
        {change.fields.map((f) => (
          <li key={f.field}>
            {f.field}: <s className="text-slate-700">{f.before}</s> <span aria-hidden="true">→</span>
            <span className="sr-only">changed to</span> <strong>{f.after}</strong>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function LegList({
  legs,
  changes = [],
  headingLevel = 3,
}: {
  legs: LegView[]
  changes?: LegChange[]
  headingLevel?: 3 | 4
}) {
  const total = totalCents(legs)
  const H = headingLevel === 3 ? 'h3' : 'h4'
  const removed = changes.filter((c) => c.kind === 'removed')
  return (
    <div>
      <ol className="space-y-3" aria-label="Payments">
        {legs.map((leg, i) => {
          const change = changes.find((c) => c.position === i + 1 && c.kind !== 'removed')
          return (
            <li key={`${leg.legId}-${i}`} className="rounded-lg border border-slate-300 p-3">
              <H className="text-sm font-semibold uppercase tracking-wide text-slate-700">
                Payment {i + 1} of {legs.length} · {actionLabel(leg.action)}
              </H>
              <p className="mt-1 text-lg text-slate-900">
                {TARGET_LABEL[leg.action] ?? 'To'}: <strong>{leg.displayName}</strong>
                {leg.maskedSuffix && <>, account ending <strong>{leg.maskedSuffix}</strong></>}
              </p>
              <p className="mt-1 text-3xl font-bold tabular-nums text-slate-950">{formatMoney(leg.amount, leg.currency)}</p>
              <dl className="mt-1 grid grid-cols-[auto,1fr] gap-x-2 text-base text-slate-800">
                <dt className="font-semibold">From:</dt>
                <dd>{leg.sourceDisplay}</dd>
                {leg.reference && (
                  <>
                    <dt className="font-semibold">Reference:</dt>
                    <dd className="break-words">{leg.reference}</dd>
                  </>
                )}
              </dl>
              <EquityDetails leg={leg} />
              {change && <ChangeNote change={change} />}
            </li>
          )
        })}
      </ol>
      {removed.length > 0 && (
        <div className="mt-3 rounded-lg border-l-4 border-indigo-700 bg-indigo-50 p-2 text-base text-indigo-950">
          <p className="font-semibold">Removed since the last draft:</p>
          <ul>
            {removed.map((c) => (
              <li key={c.position}>
                <s>{c.label}</s>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-4 flex flex-wrap items-baseline justify-between gap-2 border-t-2 border-slate-900 pt-3">
        <span className="text-lg font-semibold">Total debit</span>
        <span className="text-3xl font-bold tabular-nums">
          {total === null ? 'unavailable' : `SGD ${formatCents(total)}`}
        </span>
      </p>
    </div>
  )
}
