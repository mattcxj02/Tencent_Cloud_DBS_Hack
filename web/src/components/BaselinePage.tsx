// Conventional form/menu payment flow for the T-14 usability comparison.
// UI-only: an in-memory mock with the same seed payees/accounts as the simulated bank.
// It calls no gateway API and moves no money (not even simulated money).
import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { formatCents, isPositiveMoney, toCents } from '../money'
import { FacilitatorPanel } from './FacilitatorPanel'
import { Button, Field, Notice, inputClass } from './ui'

interface Target {
  id: string
  group: 'Saved payees' | 'PayNow' | 'Bills' | 'Credit cards' | 'My accounts'
  label: string
  detail: string
  kind: 'transfer' | 'bill' | 'card' | 'own'
  outstandingCents?: number
  reference?: string
}

interface MockAccount {
  id: string
  label: string
  cents: number
}

// Same synthetic seed as ledger/seed.py (user-matthew).
const SEED_ACCOUNTS: MockAccount[] = [
  { id: 'acct-matthew-savings', label: 'Savings ending 1234', cents: 1_200_000 },
  { id: 'acct-matthew-current', label: 'Current ending 5678', cents: 350_000 },
  { id: 'acct-joint-01', label: 'Joint ending 9012', cents: 200_000 },
]

const TARGETS: Target[] = [
  { id: 'payee-mum', group: 'Saved payees', label: 'Mum', detail: 'DBS ending 9988', kind: 'transfer' },
  { id: 'payee-ahma', group: 'Saved payees', label: 'Ah Ma', detail: 'POSB ending 7766', kind: 'transfer' },
  { id: 'payee-johndoe', group: 'Saved payees', label: 'John Doe', detail: 'DBS ending 4521', kind: 'transfer' },
  { id: 'payee-johntan', group: 'Saved payees', label: 'John Tan', detail: 'OCBC ending 8892', kind: 'transfer' },
  { id: 'payee-lim', group: 'Saved payees', label: 'Landlord Lim', detail: 'DBS ending 3344', kind: 'transfer' },
  { id: 'payee-northwind', group: 'Saved payees', label: 'Northwind Trading', detail: 'CITI ending 1100', kind: 'transfer' },
  { id: 'payee-priya', group: 'PayNow', label: 'Priya', detail: 'PayNow ending **12', kind: 'transfer' },
  { id: 'biller-spgroup', group: 'Bills', label: 'SP Group', detail: 'Bill ref SP-REF-2026-001', kind: 'bill', reference: 'SP-REF-2026-001' },
  { id: 'biller-singtel', group: 'Bills', label: 'Singtel', detail: 'Bill ref SG-REF-2026-002', kind: 'bill', reference: 'SG-REF-2026-002' },
  { id: 'card-matthew-01', group: 'Credit cards', label: 'Credit card ending 4242', detail: 'Outstanding SGD 642.30', kind: 'card', outstandingCents: 64_230 },
  ...SEED_ACCOUNTS.map((a) => ({ id: a.id, group: 'My accounts' as const, label: a.label, detail: 'Own account', kind: 'own' as const })),
]

const GROUPS: Target['group'][] = ['Saved payees', 'PayNow', 'Bills', 'Credit cards', 'My accounts']

type Step = 'payee' | 'amount' | 'account' | 'review' | 'done'

const STEP_TITLE: Record<Step, string> = {
  payee: 'Step 1 of 4: Choose who to pay',
  amount: 'Step 2 of 4: Enter the amount',
  account: 'Step 3 of 4: Choose the account to pay from',
  review: 'Step 4 of 4: Review and confirm',
  done: 'Payment recorded (baseline mock)',
}

interface Done {
  n: number
  to: string
  from: string
  cents: number
}

export function BaselinePage() {
  const [accounts, setAccounts] = useState<MockAccount[]>(SEED_ACCOUNTS)
  const [step, setStep] = useState<Step>('payee')
  const [targetId, setTargetId] = useState('')
  const [amount, setAmount] = useState('')
  const [accountId, setAccountId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<Done[]>([])
  const headingRef = useRef<HTMLHeadingElement>(null)

  const target = TARGETS.find((t) => t.id === targetId) ?? null
  const account = accounts.find((a) => a.id === accountId) ?? null
  const sourceChoices = useMemo(() => accounts.filter((a) => a.id !== targetId), [accounts, targetId])

  useEffect(() => {
    document.title = 'Baseline prototype — DCTA'
    headingRef.current?.focus()
  }, [step])

  const go = (next: Step) => {
    setError(null)
    setStep(next)
  }

  const next = (e: FormEvent) => {
    e.preventDefault()
    if (step === 'payee') {
      if (!target) return setError('Choose who to pay.')
      return go('amount')
    }
    if (step === 'amount') {
      if (!isPositiveMoney(amount)) return setError('Enter an amount in SGD, for example 50 or 50.00.')
      if (target?.outstandingCents !== undefined && toCents(amount) > target.outstandingCents) {
        return setError(`The card's outstanding balance is SGD ${formatCents(target.outstandingCents)}.`)
      }
      return go('account')
    }
    if (step === 'account') {
      if (!account) return setError('Choose an account.')
      if (toCents(amount) > account.cents) return setError('Not enough money in that account.')
      return go('review')
    }
  }

  const confirm = () => {
    if (!target || !account) return
    const cents = toCents(amount)
    setAccounts((list) =>
      list.map((a) =>
        a.id === account.id ? { ...a, cents: a.cents - cents } : a.id === target.id ? { ...a, cents: a.cents + cents } : a,
      ),
    )
    setDone((d) => [...d, { n: d.length + 1, to: target.label, from: account.label, cents }])
    go('done')
  }

  const another = () => {
    setTargetId('')
    setAmount('')
    setAccountId('')
    go('payee')
  }

  const reset = () => {
    setAccounts(SEED_ACCOUNTS)
    setDone([])
    another()
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border-4 border-dashed border-amber-600 bg-amber-50 p-3">
        <h1 className="text-2xl font-bold">Baseline prototype (no money moves)</h1>
        <p className="text-base">
          A conventional menu-and-form payment flow used only to compare usability with the assistant. It does not
          contact the bank service.
        </p>
      </div>

      <section aria-labelledby="baseline-step" className="rounded-xl border border-slate-300 bg-white p-4">
        <h2 id="baseline-step" ref={headingRef} tabIndex={-1} className="mb-3 text-xl font-bold">
          {STEP_TITLE[step]}
        </h2>

        {step === 'done' ? (
          <div className="space-y-3">
            <Notice kind="success" title="Payment recorded in the baseline mock">
              <p>No money moved. This prototype is not connected to any bank.</p>
            </Notice>
            <div className="flex flex-wrap gap-2">
              <Button onClick={another}>Make another payment</Button>
            </div>
          </div>
        ) : step === 'review' && target && account ? (
          <div className="space-y-3">
            <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-lg">
              <dt className="font-semibold">To</dt>
              <dd>
                {target.label} ({target.detail})
              </dd>
              <dt className="font-semibold">Amount</dt>
              <dd className="text-2xl font-bold tabular-nums">SGD {formatCents(toCents(amount))}</dd>
              <dt className="font-semibold">From</dt>
              <dd>{account.label}</dd>
              {target.reference && (
                <>
                  <dt className="font-semibold">Reference</dt>
                  <dd>{target.reference}</dd>
                </>
              )}
            </dl>
            <div className="flex flex-wrap gap-2">
              <Button variant="approve" onClick={confirm}>
                Confirm payment
              </Button>
              <Button variant="secondary" onClick={() => go('account')}>
                Back
              </Button>
              <Button variant="danger" onClick={another}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <form onSubmit={next} className="space-y-3" noValidate>
            {step === 'payee' &&
              GROUPS.map((g) => (
                <fieldset key={g} className="space-y-1">
                  <legend className="text-base font-bold">{g}</legend>
                  {TARGETS.filter((t) => t.group === g).map((t) => (
                    <label key={t.id} className="flex min-h-[44px] items-center gap-3 rounded-lg border border-slate-300 px-3">
                      <input
                        type="radio"
                        name="baseline-target"
                        className="h-5 w-5"
                        checked={targetId === t.id}
                        onChange={() => setTargetId(t.id)}
                      />
                      <span>
                        <strong>{t.label}</strong> <span className="text-slate-700">· {t.detail}</span>
                      </span>
                    </label>
                  ))}
                </fieldset>
              ))}
            {step === 'amount' && (
              <Field id="baseline-amount" label={`Amount to pay ${target?.label ?? ''} (SGD)`}>
                <input
                  id="baseline-amount"
                  className={`${inputClass} max-w-[14rem] text-xl`}
                  inputMode="decimal"
                  autoComplete="off"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </Field>
            )}
            {step === 'account' && (
              <fieldset className="space-y-1">
                <legend className="text-base font-bold">Pay from</legend>
                {sourceChoices.map((a) => (
                  <label key={a.id} className="flex min-h-[44px] items-center gap-3 rounded-lg border border-slate-300 px-3">
                    <input
                      type="radio"
                      name="baseline-account"
                      className="h-5 w-5"
                      checked={accountId === a.id}
                      onChange={() => setAccountId(a.id)}
                    />
                    <span>
                      <strong>{a.label}</strong> <span className="text-slate-700">· balance SGD {formatCents(a.cents)}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
            )}
            {error && <Notice kind="error" title={error} focus />}
            <div className="flex flex-wrap gap-2">
              <Button type="submit">Next</Button>
              {step !== 'payee' && (
                <Button variant="secondary" onClick={() => go(step === 'account' ? 'amount' : 'payee')}>
                  Back
                </Button>
              )}
            </div>
          </form>
        )}
      </section>

      {done.length > 0 && (
        <section aria-labelledby="baseline-done" className="rounded-xl border border-slate-300 bg-white p-4">
          <h2 id="baseline-done" className="text-lg font-bold">
            Payments made in this session (mock)
          </h2>
          <ul className="mt-2 space-y-1">
            {done.map((d) => (
              <li key={d.n}>
                {d.n}. SGD {formatCents(d.cents)} to {d.to} from {d.from}
              </li>
            ))}
          </ul>
          <p className="mt-2 font-semibold">
            Total: SGD {formatCents(done.reduce((s, d) => s + d.cents, 0))}
          </p>
        </section>
      )}

      <FacilitatorPanel scope="Baseline form" />
      <div>
        <Button variant="ghost" onClick={reset}>
          Reset baseline mock (balances and history)
        </Button>
      </div>
    </div>
  )
}
