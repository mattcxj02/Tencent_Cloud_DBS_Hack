// Simulated secondary approval: a one-time code "sent to your registered phone".
// Kept visually separate from the passkey confirmation. The code is checked by the
// gateway, which then rechecks policy and commits atomically (or blocks).
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from '../api'
import type { ApproveResult, AuthorizationView, StepUpStatus } from '../api'
import { describeCode, describeCodes, splitCode } from '../messages'
import { formatSeconds } from '../format'
import { useCountdown } from '../hooks'
import { useAnnounce } from '../session'
import { Button, Card, Field, Notice, inputClass } from './ui'

export interface StepUpOutcome {
  result?: ApproveResult
  expired?: boolean
  lost?: boolean
  cancelled?: boolean
}

export function StepUp({
  authz,
  reasonCodes,
  skewMs,
  onOutcome,
}: {
  authz: AuthorizationView
  reasonCodes: string[]
  skewMs: number
  onOutcome: (o: StepUpOutcome) => void
}) {
  const announce = useAnnounce()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const [status, setStatus] = useState<StepUpStatus | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState<null | 'submit' | 'resend' | 'cancel'>(null)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const inFlight = useRef(false)

  const expiresAt = status?.expires_at ?? authz.expires_at
  const remaining = useCountdown(expiresAt, skewMs)

  useEffect(() => {
    headingRef.current?.focus()
    announce('Extra check needed. Enter the one-time code sent to your registered phone (simulated).')
    api
      .stepUpStatus(authz.authorization_id)
      .then(setStatus)
      .catch((e) => setError(e instanceof ApiError ? describeCode(e.code) : 'Could not load the code status.'))
  }, [authz.authorization_id, announce])

  useEffect(() => {
    if (remaining === 0 && busy === null) onOutcome({ expired: true })
  }, [remaining, busy, onOutcome])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (inFlight.current) return
    if (!/^\d{6}$/.test(code)) {
      setError('Enter the 6-digit code.')
      return
    }
    inFlight.current = true
    setBusy('submit')
    setError(null)
    setInfo(null)
    try {
      const result = await api.stepUpSubmit(authz.authorization_id, code, authz.envelope_hash)
      onOutcome({ result })
    } catch (err) {
      if (!(err instanceof ApiError)) {
        setError('Something went wrong. Please try again.')
      } else if (err.network) {
        onOutcome({ lost: true })
      } else {
        const { base } = splitCode(err.code)
        const left = typeof err.body?.attempts_remaining === 'number' ? err.body.attempts_remaining : null
        if (base === 'STEP_UP_CODE_INVALID') {
          setCode('')
          setError(`${describeCode(base)}${left !== null ? ` ${left} ${left === 1 ? 'attempt' : 'attempts'} left.` : ''}`)
          if (left !== null) setStatus((s) => (s ? { ...s, attempts_remaining: left } : s))
        } else if (base === 'STEP_UP_EXPIRED' || base === 'AUTHORIZATION_EXPIRED') {
          onOutcome({ expired: true })
        } else if (base === 'STEP_UP_ATTEMPTS_EXHAUSTED' || base === 'STEP_UP_BINDING_MISMATCH') {
          onOutcome({ result: { authorization_id: authz.authorization_id, status: 'BLOCKED', reason_codes: [base] } })
        } else if (base === 'NOT_PENDING' || base === 'STEP_UP_NOT_ACTIVE' || base === 'STEP_UP_NOT_REQUIRED') {
          onOutcome({ lost: true }) // read the real state instead of guessing
        } else {
          setError(describeCode(err.code))
        }
      }
    } finally {
      inFlight.current = false
      setBusy(null)
    }
  }

  const resend = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy('resend')
    setError(null)
    try {
      setStatus(await api.stepUpResend(authz.authorization_id))
      setInfo('The same code was sent again (simulated). The time limit does not change.')
      announce('Code sent again.')
    } catch (err) {
      setError(err instanceof ApiError ? describeCode(err.code) : 'Could not resend the code.')
    } finally {
      inFlight.current = false
      setBusy(null)
    }
  }

  const cancel = async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy('cancel')
    try {
      await api.cancelAuthorization(authz.authorization_id)
      onOutcome({ cancelled: true })
    } catch (err) {
      if (err instanceof ApiError && !err.network && splitCode(err.code).base !== 'NOT_PENDING') {
        setError(describeCode(err.code))
      } else {
        onOutcome({ lost: true })
      }
    } finally {
      inFlight.current = false
      setBusy(null)
    }
  }

  return (
    <Card title="Extra check: enter your one-time code" titleId="stepup-heading" headingRef={headingRef}>
      <div className="space-y-3">
        <Notice kind="info" title="Simulated phone code">
          <p>A 6-digit code was sent to your registered phone (simulated).</p>
          <p className="text-sm">
            {status?.channel ??
              'Simulated secondary channel (demo operator console). Not a real SMS; no bank channel is involved.'}
          </p>
        </Notice>
        {reasonCodes.length > 0 && (
          <div>
            <p className="font-semibold">Why we are asking:</p>
            <ul className="ml-5 list-disc">
              {describeCodes(reasonCodes).map((r) => (
                <li key={r.code}>{r.message}</li>
              ))}
            </ul>
          </div>
        )}
        <p className="text-base" role="timer" aria-live="off">
          Time left: <strong className="tabular-nums">{remaining === null ? '—' : formatSeconds(remaining)}</strong>
          {status && (
            <>
              {' '}
              · Attempts left: <strong>{status.attempts_remaining}</strong>
            </>
          )}
        </p>
        <form onSubmit={submit} className="space-y-3" noValidate>
          <Field id="stepup-code" label="One-time code" hint="6 digits">
            <input
              id="stepup-code"
              aria-describedby="stepup-code-hint"
              className={`${inputClass} max-w-[12rem] text-2xl tracking-[0.3em]`}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              aria-invalid={error ? true : undefined}
            />
          </Field>
          {error && <Notice kind="error" title={error} focus />}
          {info && <Notice kind="success" title={info} />}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="approve" busy={busy === 'submit'} busyLabel="Checking code…" disabled={busy !== null}>
              Submit code
            </Button>
            <Button variant="secondary" onClick={resend} busy={busy === 'resend'} disabled={busy !== null}>
              Resend code
            </Button>
            <Button variant="danger" onClick={cancel} busy={busy === 'cancel'} disabled={busy !== null}>
              Cancel payment
            </Button>
          </div>
        </form>
      </div>
    </Card>
  )
}
