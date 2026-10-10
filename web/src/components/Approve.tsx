import { useState } from 'react'
import { startAuthentication } from '@simplewebauthn/browser'

type RequestOptionsJSON = Parameters<typeof startAuthentication>[0]['optionsJSON']

const API_BASE = '/api'

// The exact envelope the gateway stored. The user signs a hash of these bytes,
// so the screen renders these fields and nothing produced by a model.
interface EnvelopeLeg {
  leg_id: string
  action: string
  source_account_id: string
  target_type: string
  amount: string
  currency: string
  reference: string | null
  display_metadata: { display_name: string; masked_account_suffix: string | null }
  equity_terms: {
    budget: string
    unit_price: string
    quantity: number
    actual_debit: string
    residual_cash: string
    quote_expires_at: string
  } | null
}

interface AuthorizationView {
  authorization_id: string
  status: string
  envelope_hash: string
  expires_at: string
  envelope: { operation: string; payload: { legs: EnvelopeLeg[] } }
  webauthn_options: RequestOptionsJSON
}

const ACTION_LABEL: Record<string, string> = {
  transfer: 'Transfer',
  pay_bill: 'Pay bill',
  pay_card: 'Pay card',
  buy_equity: 'Buy shares',
}

// Money arrives as 2-dp strings; add in integer cents, never floats.
function totalDebit(legs: EnvelopeLeg[]): string {
  const cents = legs.reduce((sum, leg) => {
    const [whole, frac = '0'] = leg.amount.split('.')
    return sum + Number(whole) * 100 + Number(frac.padEnd(2, '0'))
  }, 0)
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`
}

async function errorDetail(resp: Response): Promise<string> {
  const data = await resp.json().catch(() => ({ detail: resp.statusText }))
  return typeof data.detail === 'string' ? data.detail : `HTTP ${resp.status}`
}

export function Approve() {
  const [authzId, setAuthzId] = useState('')
  const [view, setView] = useState<AuthorizationView | null>(null)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')

  const fetchAuthorization = async () => {
    setStatus('')
    setError('')
    const resp = await fetch(`${API_BASE}/authorizations/${encodeURIComponent(authzId.trim())}`)
    if (!resp.ok) {
      setError(await errorDetail(resp))
      return
    }
    setView(await resp.json())
  }

  const approve = async () => {
    if (!view) return
    setError('')
    try {
      setStatus('Waiting for Touch ID / passkey…')
      // Sign the challenge derived from the stored envelope, not a login challenge.
      const assertion = await startAuthentication({ optionsJSON: view.webauthn_options })
      setStatus('Submitting approval…')
      const resp = await fetch(`${API_BASE}/authorizations/${view.authorization_id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          authorization_id: view.authorization_id,
          expected_envelope_hash: view.envelope_hash,
          assertion,
        }),
      })
      if (!resp.ok) {
        throw new Error(await errorDetail(resp))
      }
      const result = await resp.json()
      setStatus(`Status: ${result.status}${result.reason ? ` (${result.reason})` : ''}`)
    } catch (e) {
      setStatus('')
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const legs = view?.envelope.payload.legs ?? []

  return (
    <section className="bg-white rounded-lg shadow p-4" aria-labelledby="approve-heading">
      <h2 id="approve-heading" className="text-sm font-medium text-gray-600 mb-2">
        Approve transaction
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        Simulated bank. Review every line below: your passkey signs exactly these details.
      </p>
      <div className="flex gap-2 mb-3">
        <label htmlFor="authz-id" className="sr-only">
          Authorization ID
        </label>
        <input
          id="authz-id"
          type="text"
          value={authzId}
          onChange={(e) => setAuthzId(e.target.value)}
          placeholder="Authorization ID"
          className="flex-1 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={fetchAuthorization}
          disabled={!authzId.trim()}
          className="bg-gray-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-gray-700 disabled:opacity-50"
        >
          Load
        </button>
      </div>

      {view && (
        <div className="border rounded p-3 bg-gray-50 space-y-3 text-sm">
          <ol className="space-y-2">
            {legs.map((leg) => (
              <li key={leg.leg_id} className="border-b pb-2 last:border-b-0">
                <p className="font-medium">
                  {ACTION_LABEL[leg.action] ?? leg.action}: {leg.display_metadata.display_name}
                  {leg.display_metadata.masked_account_suffix &&
                    ` (ending ${leg.display_metadata.masked_account_suffix})`}
                </p>
                <p className="text-lg font-semibold">
                  {leg.currency} {leg.amount}
                </p>
                <p className="text-gray-500">From {leg.source_account_id}</p>
                {leg.reference && <p className="text-gray-500">Reference: {leg.reference}</p>}
                {leg.equity_terms && (
                  <p className="text-gray-500">
                    {leg.equity_terms.quantity} shares at SGD {leg.equity_terms.unit_price} (budget{' '}
                    {leg.equity_terms.budget}, unspent {leg.equity_terms.residual_cash}; whole
                    shares only)
                  </p>
                )}
              </li>
            ))}
          </ol>
          <p className="text-base">
            Total debit: <strong>SGD {totalDebit(legs)}</strong>
          </p>
          <p className="text-gray-500">
            Status: {view.status} · Expires {view.expires_at}
          </p>
          <button
            onClick={approve}
            disabled={view.status !== 'PENDING_APPROVAL'}
            className="w-full bg-green-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-green-700 disabled:opacity-50"
          >
            Approve with passkey
          </button>
        </div>
      )}

      <p role="status" aria-live="polite" className="mt-2 text-sm text-blue-600">
        {status}
      </p>
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-600">
          {error}
        </p>
      )}
    </section>
  )
}
