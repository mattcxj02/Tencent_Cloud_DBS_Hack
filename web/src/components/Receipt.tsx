// The stored receipt of a committed authorization. Shown only for status COMMITTED,
// so a failed or unknown attempt can never look like a success.
import { useEffect, useRef } from 'react'
import type { Account, CommittedResult } from '../api'
import { accountLabel, actionLabel } from '../legs'
import { formatMoney } from '../money'
import { formatDateTime } from '../format'
import { hrefFor } from '../router'
import { Button, Card, Notice } from './ui'

export function Receipt({
  result,
  recovered,
  accounts,
  conversationId,
  onNew,
}: {
  result: CommittedResult
  recovered: boolean
  accounts: Account[]
  conversationId: string | null
  onNew: () => void
}) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])
  const r = result.receipt
  return (
    <Card title="Paid (simulated bank)" titleId="receipt-heading" headingRef={headingRef}>
      <div className="space-y-3">
        <Notice kind="success" title={`${r.legs.length} ${r.legs.length === 1 ? 'payment' : 'payments'} completed`}>
          <p>
            Total debit <strong className="text-2xl tabular-nums">{formatMoney(r.total_debit)}</strong>
          </p>
          <p className="text-sm">Simulated bank: no real money moved.</p>
        </Notice>
        {(recovered || result.replayed) && (
          <Notice kind="info" title="Recovered receipt">
            The connection dropped, so we fetched the bank's stored receipt. Nothing was paid twice.
          </Notice>
        )}
        <ol className="space-y-2">
          {r.legs.map((leg) => (
            <li key={leg.leg_id} className="rounded-lg border border-slate-300 p-3">
              <p className="text-sm font-semibold uppercase tracking-wide text-slate-700">
                Payment {leg.leg_index + 1} · {actionLabel(leg.action)}
              </p>
              <p className="text-lg">
                <strong>{leg.display_name}</strong> — <strong className="tabular-nums">{formatMoney(leg.amount)}</strong>
              </p>
              {leg.quantity !== undefined && leg.unit_price && (
                <p>
                  {leg.quantity} shares at {formatMoney(leg.unit_price)} each
                </p>
              )}
              <p className="text-sm text-slate-700">From {accountLabel(accounts, leg.source_account_id)}</p>
              <p className="break-all text-sm text-slate-700">
                Transaction {leg.transaction_id}
                {leg.settlement_ref && <> · Settlement ref {leg.settlement_ref} (simulated)</>}
              </p>
            </li>
          ))}
        </ol>
        <dl className="grid grid-cols-[auto,1fr] gap-x-2 text-sm text-slate-800">
          <dt className="font-semibold">Receipt</dt>
          <dd className="break-all">{r.receipt_id}</dd>
          <dt className="font-semibold">Completed</dt>
          <dd>{formatDateTime(r.committed_at)}</dd>
          <dt className="font-semibold">Extra code used</dt>
          <dd>{r.step_up_verified ? 'Yes' : 'No'}</dd>
        </dl>
        <div className="flex flex-wrap gap-2">
          {conversationId && (
            <a
              href={hrefFor('evidence', conversationId)}
              className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-400 px-4 py-2 font-semibold text-indigo-800 hover:bg-slate-100"
            >
              View evidence trail
            </a>
          )}
          <Button onClick={onNew}>Start a new request</Button>
        </div>
      </div>
    </Card>
  )
}
