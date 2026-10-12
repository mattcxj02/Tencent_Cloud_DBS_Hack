// Passkey enrollment (one-time code from `make enroll-code`) and sign-in. The credential
// that signs in is remembered for this tab so payment envelopes are bound to it.
import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { startRegistration } from '@simplewebauthn/browser'
import { api, ApiError } from '../api'
import { describeCode } from '../messages'
import { PasskeyError, signWithPasskey } from '../passkey'
import { useAnnounce, useSession } from '../session'
import { Button, Card, Field, Notice, PageTitle, inputClass } from './ui'

function registrationMessage(e: unknown): string {
  const name = e instanceof Error ? e.name : ''
  if (name === 'NotAllowedError' || name === 'AbortError') {
    return 'Passkey setup was cancelled or timed out. Nothing was registered. Ask for a new enrollment code if this one was used.'
  }
  if (name === 'InvalidStateError') return 'This device already has a passkey for this account. Sign in instead.'
  if (e instanceof ApiError) return describeCode(e.code)
  return 'Passkey setup failed. Please try again.'
}

export function SignInPage() {
  const { completeSignIn, notice } = useSession()
  const announce = useAnnounce()
  const [busy, setBusy] = useState<null | 'signin' | 'enroll'>(null)
  const [error, setError] = useState<string | null>(null)
  const [enrolled, setEnrolled] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const signInRef = useRef<HTMLButtonElement>(null)

  const signIn = async () => {
    if (busy) return
    setBusy('signin')
    setError(null)
    try {
      const options = await api.loginBegin()
      const assertion = await signWithPasskey(options)
      const result = await api.loginVerify(assertion)
      announce('Signed in.')
      await completeSignIn(result.user_id, assertion.id)
    } catch (e) {
      if (e instanceof PasskeyError) setError(e.message)
      else if (e instanceof ApiError && (e.status === 401 || e.status === 400)) {
        setError("That passkey wasn't accepted. If this device was never enrolled, use an enrollment code below.")
      } else setError(e instanceof ApiError ? describeCode(e.code) : 'Sign-in failed. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  const enroll = async (e: FormEvent) => {
    e.preventDefault()
    if (busy || !code.trim()) return
    setBusy('enroll')
    setError(null)
    setEnrolled(null)
    try {
      const options = await api.enrollBegin(code.trim())
      const credential = await startRegistration({ optionsJSON: options })
      const result = await api.enrollVerify(credential)
      setEnrolled(result.user_id)
      setCode('')
      announce('Passkey registered. Now sign in.')
      window.setTimeout(() => signInRef.current?.focus(), 50)
    } catch (err) {
      setError(registrationMessage(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-4">
      <PageTitle sub="Prototype on a simulated bank. Your passkey (Touch ID, Windows Hello or a security key) stays on this device.">Sign in</PageTitle>
      {notice && <Notice kind="warning" title={notice} />}
      {error && <Notice kind="error" title={error} focus />}
      {enrolled && <Notice kind="success" title={`Passkey registered for ${enrolled}. Now sign in.`} />}

      <Card title="Sign in with your passkey" titleId="signin-heading">
        <p className="mb-3 text-base">Use Touch ID, Windows Hello or your device's passkey prompt.</p>
        <Button ref={signInRef} onClick={() => void signIn()} busy={busy === 'signin'} busyLabel="Waiting for your passkey…" disabled={busy !== null}>
          Sign in with passkey (Touch ID / Windows Hello)
        </Button>
      </Card>

      <Card title="First time on this device?" titleId="enroll-heading">
        <form onSubmit={enroll} className="space-y-3">
          <Field id="enroll-code" label="Enrollment code" hint="A one-time code from the bank (demo: make enroll-code).">
            <input
              id="enroll-code"
              aria-describedby="enroll-code-hint"
              className={`${inputClass} max-w-[20rem]`}
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </Field>
          <Button type="submit" variant="secondary" busy={busy === 'enroll'} busyLabel="Registering…" disabled={busy !== null || !code.trim()}>
            Register a passkey
          </Button>
        </form>
      </Card>
    </div>
  )
}
