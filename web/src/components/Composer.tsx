// Text input plus push-to-talk microphone. Voice capture lives in src/audio (voice task);
// this file only uses the hook's public contract.
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useRecorder } from '../audio/useRecorder'
import { describeCode } from '../messages'
import { Button, inputClass } from './ui'

const CONSENT_KEY = 'dcta.voice_consent'

function hasConsent(): boolean {
  try {
    return window.localStorage.getItem(CONSENT_KEY) === '1'
  } catch {
    return false
  }
}

function saveConsent(): void {
  try {
    window.localStorage.setItem(CONSENT_KEY, '1')
  } catch {
    // storage blocked: we simply ask again next time
  }
}

/** Recorder and gateway voice errors are stable codes (src/audio/messages.ts). */
function voiceMessage(raw: unknown): string {
  const code =
    raw !== null && typeof raw === 'object' && 'code' in raw && typeof (raw as { code: unknown }).code === 'string'
      ? (raw as { code: string }).code
      : raw instanceof Error
        ? raw.message
        : typeof raw === 'string'
          ? raw
          : ''
  return code ? describeCode(code) : 'Voice input failed. Please type instead.'
}

function mmss(ms: number): string {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function MicIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" strokeLinecap="round" />
    </svg>
  )
}

function MicButton({ disabled, onRecorded }: { disabled: boolean; onRecorded: (audio: Blob) => void }) {
  // At the 60 s limit the hook stops by itself and hands over the clip, so nothing is lost.
  const rec = useRecorder({ onAutoStop: onRecorded })
  const [localError, setLocalError] = useState<string | null>(null)
  const [askConsent, setAskConsent] = useState(false)
  const recording = rec.state === 'recording'
  const processing = rec.state === 'processing'

  useEffect(() => {
    if (!recording) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') rec.cancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [recording, rec])

  const begin = async () => {
    setLocalError(null)
    try {
      await rec.start()
    } catch (e) {
      setLocalError(voiceMessage(e))
    }
  }

  const toggle = async () => {
    setLocalError(null)
    if (recording) {
      try {
        onRecorded(await rec.stop())
      } catch (e) {
        setLocalError(voiceMessage(e))
      }
      return
    }
    if (!hasConsent()) {
      setAskConsent(true) // ask once, before the first recording
      return
    }
    await begin()
  }

  const agree = async () => {
    saveConsent()
    setAskConsent(false)
    await begin()
  }

  const error = localError ?? (rec.error ? voiceMessage(rec.error) : null)
  return (
    <div className="flex flex-col items-start gap-1">
      {askConsent && (
        <div role="group" aria-labelledby="voice-consent-title" className="w-full rounded-lg border-l-4 border-sky-700 bg-sky-50 p-3">
          <p id="voice-consent-title" className="font-semibold">Before you use voice</p>
          <p className="text-base">
            Your recording is stored by the (simulated) bank as evidence for 90 days, together with its transcript.
            Recordings are never sent to the tracing service. You can always type instead.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button onClick={() => void agree()}>Agree and start recording</Button>
            <Button variant="secondary" onClick={() => setAskConsent(false)}>
              Not now
            </Button>
          </div>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant={recording ? 'danger' : 'secondary'}
          onClick={() => void toggle()}
          disabled={disabled || processing || askConsent}
          aria-pressed={recording}
          busy={processing}
          busyLabel="Preparing audio…"
        >
          <MicIcon />
          {recording ? 'Stop and send' : 'Speak'}
        </Button>
        {recording && (
          <Button variant="ghost" onClick={() => rec.cancel()}>
            Discard recording
          </Button>
        )}
      </div>
      {recording && (
        <p className="text-sm text-slate-800">
          <span aria-hidden="true">● </span>Recording {mmss(rec.elapsedMs)} of {mmss(rec.maxMs)}. Press “Stop and send”
          when done (Esc discards).
        </p>
      )}
      <p aria-live="polite" className="sr-only">
        {recording ? 'Recording started.' : ''}
      </p>
      {rec.limitReached && !recording && (
        <p className="text-sm text-slate-800">The 1-minute limit was reached, so the recording was sent automatically.</p>
      )}
      {error && (
        <p role="alert" className="text-sm font-semibold text-red-800">
          <span aria-hidden="true">✕ </span>
          {error}
        </p>
      )}
    </div>
  )
}

export function Composer({
  label,
  placeholder,
  disabled,
  onSend,
  onRecorded,
}: {
  label: string
  placeholder: string
  disabled: boolean
  onSend: (text: string) => void
  onRecorded: (audio: Blob) => void
}) {
  const [text, setText] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const t = text.trim()
    if (!t || disabled) return
    onSend(t)
    setText('')
  }
  return (
    <form onSubmit={submit} className="space-y-2 rounded-xl border border-slate-300 bg-white p-3 shadow-sm">
      <label htmlFor="composer-input" className="block text-base font-semibold text-slate-900">
        {label}
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id="composer-input"
          type="text"
          value={text}
          maxLength={2000}
          autoComplete="off"
          placeholder={placeholder}
          onChange={(e) => setText(e.target.value)}
          className={`${inputClass} flex-1`}
        />
        <Button type="submit" disabled={disabled || !text.trim()}>
          Send
        </Button>
      </div>
      <MicButton disabled={disabled} onRecorded={onRecorded} />
    </form>
  )
}
