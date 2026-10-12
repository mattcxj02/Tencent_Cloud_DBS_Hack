// Saved payees with the names the assistant recognises: the bank's aliases (fixed) and
// the customer's own nicknames (add / remove). A nickname is only a name: every payment
// still shows the real payee on the confirmation screen and needs the passkey.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from '../api'
import type { Payee } from '../api'
import { describeCode } from '../messages'
import { useAnnounce } from '../session'
import { Badge, Button, Card, Field, Notice, PageTitle, inputClass } from './ui'

function where(p: Payee): string {
  if (p.kind === 'paynow') return p.masked_suffix ? `PayNow ${p.masked_suffix}` : 'PayNow'
  return [p.bank, p.masked_suffix ? `account ending ${p.masked_suffix}` : null].filter(Boolean).join(', ')
}

function PayeeCard({ payee, onChange }: { payee: Payee; onChange: (p: Payee) => void }) {
  const announce = useAnnounce()
  const [nickname, setNickname] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const inputId = `nick-${payee.payee_id}`

  const add = async (e: FormEvent) => {
    e.preventDefault()
    const text = nickname.trim()
    if (!text || busy) return
    setBusy('add')
    setError(null)
    try {
      onChange(await api.addNickname(payee.payee_id, text))
      setNickname('')
      announce(`Added nickname ${text} for ${payee.display_name}.`)
    } catch (err) {
      setError(err instanceof ApiError ? describeCode(err.code) : 'Could not add the nickname. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  const remove = async (name: string) => {
    if (busy) return
    setBusy(name)
    setError(null)
    try {
      onChange(await api.removeNickname(payee.payee_id, name))
      announce(`Removed nickname ${name}.`)
    } catch (err) {
      setError(err instanceof ApiError ? describeCode(err.code) : 'Could not remove the nickname. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card title={payee.display_name} titleId={`payee-${payee.payee_id}`}>
      <p className="text-base text-slate-700">{where(payee)}</p>

      {payee.aliases.length > 0 && (
        <div className="mt-3">
          <p className="text-sm font-semibold text-slate-900">Also recognised (set by the bank)</p>
          <ul className="mt-1 flex flex-wrap gap-2" aria-label={`Aliases for ${payee.display_name}`}>
            {payee.aliases.map((a) => (
              <li key={a}>
                <Badge>{a}</Badge>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3">
        <p className="text-sm font-semibold text-slate-900">Your nicknames</p>
        {payee.nicknames.length === 0 ? (
          <p className="text-sm text-slate-700">None yet.</p>
        ) : (
          <ul className="mt-1 flex flex-wrap gap-2" aria-label={`Nicknames for ${payee.display_name}`}>
            {payee.nicknames.map((n) => (
              <li key={n} className="inline-flex items-center gap-1 rounded-full border border-indigo-700 bg-indigo-50 pl-3 text-sm font-semibold text-indigo-950">
                {n}
                <button
                  type="button"
                  onClick={() => void remove(n)}
                  disabled={busy !== null}
                  aria-label={`Remove nickname ${n}`}
                  className="min-h-[44px] min-w-[44px] rounded-full px-2 hover:bg-indigo-100 disabled:opacity-60"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <form onSubmit={add} className="mt-3 space-y-2">
        <Field id={inputId} label="Add a nickname" hint='For example "mom", "Ah Boy" or "landlord".'>
          <input
            id={inputId}
            aria-describedby={`${inputId}-hint`}
            className={`${inputClass} max-w-[20rem]`}
            maxLength={30}
            autoComplete="off"
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
          />
        </Field>
        <Button type="submit" variant="secondary" busy={busy === 'add'} busyLabel="Adding…" disabled={busy !== null || !nickname.trim()}>
          Add nickname
        </Button>
      </form>
      {error && (
        <div className="mt-3">
          <Notice kind="error" title={error} focus />
        </div>
      )}
    </Card>
  )
}

export function PayeesPage() {
  const [payees, setPayees] = useState<Payee[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setPayees(await api.payees())
    } catch (err) {
      setError(err instanceof ApiError ? describeCode(err.code) : 'Could not load your payees.')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const replace = (updated: Payee) =>
    setPayees((list) => list?.map((p) => (p.payee_id === updated.payee_id ? updated : p)) ?? null)

  return (
    <div className="space-y-4">
      <PageTitle sub="Names the assistant recognises for each saved payee. A nickname only helps the assistant find the payee: you still check the real name on the confirmation screen and approve with your passkey.">
        Payees and nicknames
      </PageTitle>
      {error && (
        <Notice kind="error" title={error} focus>
          <Button variant="secondary" onClick={() => void load()}>
            Try again
          </Button>
        </Notice>
      )}
      {!payees && !error && <p>Loading payees…</p>}
      {payees?.map((p) => <PayeeCard key={p.payee_id} payee={p} onChange={replace} />)}
    </div>
  )
}
