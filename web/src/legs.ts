// One normalized shape for a payment leg, whether it came from a gateway draft or from
// the gateway's stored signing envelope, plus the per-leg diff between draft revisions.
import type { Account, DraftLeg, EnvelopeLeg, EquityTerms } from './api'
import { formatMoney, sumCents } from './money'

export interface LegView {
  legId: string
  action: string
  amount: string
  currency: string
  displayName: string
  maskedSuffix: string | null
  sourceAccountId: string
  sourceDisplay: string
  reference: string | null
  equity: EquityTerms | null
}

export const ACTION_LABEL: Record<string, string> = {
  transfer: 'Transfer',
  pay_bill: 'Pay bill',
  pay_card: 'Card payment',
  buy_equity: 'Buy shares',
}

export function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action
}

export function accountLabel(accounts: Account[], accountId: string, fallback?: string | null): string {
  return accounts.find((a) => a.account_id === accountId)?.display ?? fallback ?? accountId
}

export function fromDraftLeg(leg: DraftLeg, accounts: Account[]): LegView {
  return {
    legId: leg.leg_id,
    action: leg.action,
    amount: leg.amount,
    currency: leg.currency || 'SGD',
    displayName: leg.display_name,
    maskedSuffix: leg.masked_account_suffix,
    sourceAccountId: leg.source_account_id,
    sourceDisplay: leg.source_display ?? accountLabel(accounts, leg.source_account_id),
    reference: leg.reference,
    equity: leg.equity_terms,
  }
}

/** The envelope carries IDs, not labels; source labels come from the gateway's account list. */
export function fromEnvelopeLeg(leg: EnvelopeLeg, accounts: Account[]): LegView {
  return {
    legId: leg.leg_id,
    action: leg.action,
    amount: leg.amount,
    currency: leg.currency || 'SGD',
    displayName: leg.display_metadata.display_name,
    maskedSuffix: leg.display_metadata.masked_account_suffix,
    sourceAccountId: leg.source_account_id,
    sourceDisplay: accountLabel(accounts, leg.source_account_id),
    reference: leg.reference,
    equity: leg.equity_terms,
  }
}

export function recipientText(leg: Pick<LegView, 'displayName' | 'maskedSuffix'>): string {
  return leg.maskedSuffix ? `${leg.displayName}, ending ${leg.maskedSuffix}` : leg.displayName
}

/** Total debit in integer cents; null if any amount is unreadable (never guessed). */
export function totalCents(legs: LegView[]): number | null {
  try {
    return sumCents(legs.map((l) => l.amount))
  } catch {
    return null
  }
}

// ── Changes between draft revisions ──────────────────────────────────

export interface FieldChange {
  field: string
  before: string
  after: string
}

export interface LegChange {
  position: number // 1-based payment number in the new draft (or the old one, if removed)
  kind: 'changed' | 'added' | 'removed'
  label: string
  fields: FieldChange[]
}

function legTitle(leg: LegView): string {
  return `${actionLabel(leg.action)} to ${recipientText(leg)}`
}

function compare(prev: LegView, next: LegView): FieldChange[] {
  const out: FieldChange[] = []
  const add = (field: string, before: string, after: string) => {
    if (before !== after) out.push({ field, before, after })
  }
  add('Type', actionLabel(prev.action), actionLabel(next.action))
  add('Recipient', recipientText(prev), recipientText(next))
  add('Amount', formatMoney(prev.amount, prev.currency), formatMoney(next.amount, next.currency))
  add('From', prev.sourceDisplay, next.sourceDisplay)
  add('Reference', prev.reference ?? 'none', next.reference ?? 'none')
  if (prev.equity || next.equity) {
    add('Shares', String(prev.equity?.quantity ?? 0), String(next.equity?.quantity ?? 0))
  }
  return out
}

/** Payments are matched by position: the order is part of what the user asked for. */
export function diffLegs(prev: LegView[], next: LegView[]): LegChange[] {
  const changes: LegChange[] = []
  const n = Math.max(prev.length, next.length)
  for (let i = 0; i < n; i++) {
    const a = prev[i]
    const b = next[i]
    if (a && b) {
      const fields = compare(a, b)
      if (fields.length) changes.push({ position: i + 1, kind: 'changed', label: legTitle(b), fields })
    } else if (b) {
      changes.push({ position: i + 1, kind: 'added', label: legTitle(b), fields: [] })
    } else if (a) {
      changes.push({ position: i + 1, kind: 'removed', label: legTitle(a), fields: [] })
    }
  }
  return changes
}
