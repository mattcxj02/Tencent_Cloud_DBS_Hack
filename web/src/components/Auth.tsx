import { useState } from 'react'
import {
  startRegistration,
  startAuthentication,
} from '@simplewebauthn/browser'

const API_BASE = '/api'

export function Enroll() {
  const [code, setCode] = useState('')
  const [status, setStatus] = useState<string>('')
  const [error, setError] = useState<string>('')

  const enroll = async () => {
    setStatus('Beginning enrollment…')
    setError('')

    try {
      const beginResp = await fetch(`${API_BASE}/auth/enroll/begin`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      })
      if (!beginResp.ok) {
        const data = await beginResp.json().catch(() => ({ detail: beginResp.statusText }))
        throw new Error(data.detail || `HTTP ${beginResp.status}`)
      }
      const options = await beginResp.json()

      setStatus('Waiting for authenticator…')
      const attResp = await startRegistration({ optionsJSON: options })

      setStatus('Verifying…')
      const verifyResp = await fetch(`${API_BASE}/auth/enroll/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: attResp }),
      })
      if (!verifyResp.ok) {
        const data = await verifyResp.json().catch(() => ({ detail: verifyResp.statusText }))
        throw new Error(data.detail || `HTTP ${verifyResp.status}`)
      }

      const result = await verifyResp.json()
      setStatus(`Enrolled as ${result.user_id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setStatus('')
    }
  }

  return (
    <section className="bg-white rounded-lg shadow p-4">
      <h2 className="text-sm font-medium text-gray-600 mb-2">Enroll Device (WebAuthn)</h2>
      <p className="text-xs text-gray-500 mb-3">
        Enter your enrollment code to register a new authenticator.
      </p>
      <div className="flex gap-2">
        <input
          type="text"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="Enrollment code"
          className="flex-1 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={enroll}
          disabled={!code.trim()}
          className="bg-blue-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          Enroll
        </button>
      </div>
      {status && <p className="mt-2 text-sm text-blue-600">{status}</p>}
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  )
}

export function SignIn() {
  const [status, setStatus] = useState<string>('')
  const [error, setError] = useState<string>('')
  const [signedIn, setSignedIn] = useState(false)

  const signIn = async () => {
    setStatus('Beginning login…')
    setError('')

    try {
      const beginResp = await fetch(`${API_BASE}/auth/login/begin`, {
        method: 'POST',
      })
      if (!beginResp.ok) {
        const data = await beginResp.json().catch(() => ({ detail: beginResp.statusText }))
        throw new Error(data.detail || `HTTP ${beginResp.status}`)
      }
      const options = await beginResp.json()

      setStatus('Waiting for authenticator…')
      const asseResp = await startAuthentication({ optionsJSON: options })

      setStatus('Verifying…')
      const verifyResp = await fetch(`${API_BASE}/auth/login/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assertion: asseResp }),
      })
      if (!verifyResp.ok) {
        const data = await verifyResp.json().catch(() => ({ detail: verifyResp.statusText }))
        throw new Error(data.detail || `HTTP ${verifyResp.status}`)
      }

      const result = await verifyResp.json()
      setStatus(`Signed in as ${result.user_id}`)
      setSignedIn(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setStatus('')
    }
  }

  const signOut = async () => {
    await fetch(`${API_BASE}/auth/logout`, { method: 'POST' })
    setStatus('')
    setSignedIn(false)
  }

  return (
    <section className="bg-white rounded-lg shadow p-4">
      <h2 className="text-sm font-medium text-gray-600 mb-2">Sign In (WebAuthn)</h2>
      {signedIn ? (
        <button
          onClick={signOut}
          className="bg-gray-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-gray-700"
        >
          Sign Out
        </button>
      ) : (
        <button
          onClick={signIn}
          className="bg-blue-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-blue-700"
        >
          Sign In
        </button>
      )}
      {status && <p className="mt-2 text-sm text-blue-600">{status}</p>}
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  )
}
