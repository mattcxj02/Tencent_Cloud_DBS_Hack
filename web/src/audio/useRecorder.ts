// Push-to-talk microphone capture (T-12): AudioWorklet → mono float PCM → 16 kHz 16-bit WAV.
//
// Contract: useRecorder(): { state, error, start(): Promise<void>, stop(): Promise<Blob>, cancel(): void }
// plus `elapsedMs`, `maxMs` and `limitReached`. `error` is a stable code (see ./messages.ts).
//
// - start() asks for the microphone and begins capture. On failure it sets state 'error' with
//   a code (MIC_PERMISSION_DENIED, MIC_NOT_FOUND, MIC_IN_USE, VOICE_UNSUPPORTED,
//   RECORDER_FAILED) and rejects with a RecorderError carrying the same code.
// - stop() ends capture, releases the microphone and resolves with the WAV Blob
//   (state 'processing' while encoding, then 'idle'). Rejects with RECORDING_EMPTY or
//   NOT_RECORDING.
// - Capture stops by itself at 60 s: the microphone is released, `limitReached` becomes true,
//   `onAutoStop(blob)` is called if given, and a later stop() resolves with the same Blob.
// - cancel() and unmount release everything and discard the audio.
// "Released" means: every MediaStreamTrack stopped, nodes disconnected, AudioContext closed.
import { useCallback, useEffect, useRef, useState } from 'react'
import { MAX_CLIP_MS, concatFloat32, toWavClip } from './wav'

export type RecorderState = 'idle' | 'recording' | 'processing' | 'error'

export type RecorderErrorCode =
  | 'MIC_PERMISSION_DENIED'
  | 'MIC_NOT_FOUND'
  | 'MIC_IN_USE'
  | 'VOICE_UNSUPPORTED'
  | 'RECORDER_FAILED'
  | 'RECORDING_EMPTY'
  | 'NOT_RECORDING'
  | 'RECORDING_CANCELLED'

export class RecorderError extends Error {
  readonly code: RecorderErrorCode
  constructor(code: RecorderErrorCode) {
    super(code)
    this.name = 'RecorderError'
    this.code = code
  }
}

export interface Recorder {
  state: RecorderState
  error: string | null
  start(): Promise<void>
  stop(): Promise<Blob>
  cancel(): void
  /** Captured time so far (updated a few times per second while recording). */
  elapsedMs: number
  /** Hard limit for one clip. */
  maxMs: number
  /** True after capture ended by itself at `maxMs`; call stop() to collect the clip. */
  limitReached: boolean
}

export interface RecorderOptions {
  /** Clip limit in ms; capped at 60 s (TRTC sentence recognition). */
  maxMs?: number
  /** Called with the clip when capture stops by itself at the limit. */
  onAutoStop?: (blob: Blob) => void
}

const WORKLET_URL = `${import.meta.env.BASE_URL}audio-recorder-worklet.js`
const WORKLET_NAME = 'dcta-recorder'
const FLUSH_TIMEOUT_MS = 500

interface Session {
  id: number
  ctx: AudioContext | null
  stream: MediaStream | null
  source: MediaStreamAudioSourceNode | null
  node: AudioWorkletNode | null
  sink: GainNode | null
  chunks: Float32Array[]
  frames: number
  maxFrames: number
  inputRate: number
  workletDone: boolean
  onWorkletDone: (() => void) | null
  ticker: number | null
  backupTimer: number | null
  released: boolean
  cancelled: boolean
  clip: Promise<Blob> | null
}

function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof AudioContext !== 'undefined' &&
    typeof AudioWorkletNode !== 'undefined'
  )
}

function mediaErrorCode(err: unknown): RecorderErrorCode {
  const name =
    err !== null && typeof err === 'object' && 'name' in err ? String((err as { name: unknown }).name) : ''
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return 'MIC_PERMISSION_DENIED'
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return 'MIC_NOT_FOUND'
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return 'MIC_IN_USE'
    default:
      return 'RECORDER_FAILED'
  }
}

/**
 * Stop the microphone and audio graph. Every step is idempotent and runs on every call, so a
 * stream that arrives after a cancel (permission prompt still open) is still stopped.
 */
function release(s: Session): void {
  s.released = true
  if (s.ticker !== null) window.clearInterval(s.ticker)
  if (s.backupTimer !== null) window.clearTimeout(s.backupTimer)
  s.ticker = s.backupTimer = null
  try {
    s.node?.port.postMessage('stop')
  } catch {
    /* port already closed */
  }
  for (const n of [s.source, s.node, s.sink]) {
    try {
      n?.disconnect()
    } catch {
      /* already disconnected */
    }
  }
  if (s.node) s.node.port.onmessage = null
  s.stream?.getTracks().forEach((t) => t.stop())
  if (s.ctx && s.ctx.state !== 'closed') s.ctx.close().catch(() => undefined)
  s.ctx = null
  s.onWorkletDone?.()
}

export function useRecorder(options: RecorderOptions = {}): Recorder {
  const maxMs = Math.min(options.maxMs ?? MAX_CLIP_MS, MAX_CLIP_MS)
  const [state, setState] = useState<RecorderState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [limitReached, setLimitReached] = useState(false)
  const sessionRef = useRef<Session | null>(null)
  const counterRef = useRef(0)
  const onAutoStopRef = useRef(options.onAutoStop)
  onAutoStopRef.current = options.onAutoStop

  const fail = useCallback((code: RecorderErrorCode): RecorderError => {
    setState('error')
    setError(code)
    return new RecorderError(code)
  }, [])

  /** End capture (once per session) and encode the clip. */
  const finish = useCallback((s: Session, auto: boolean): Promise<Blob> => {
    if (s.clip) return s.clip
    if (sessionRef.current === s) setState('processing')
    s.clip = (async () => {
      if (!s.workletDone && !s.released) {
        await new Promise<void>((resolve) => {
          s.onWorkletDone = resolve
          window.setTimeout(resolve, FLUSH_TIMEOUT_MS)
          try {
            s.node?.port.postMessage('stop')
          } catch {
            resolve()
          }
        })
      }
      release(s)
      if (s.cancelled) throw new RecorderError('RECORDING_CANCELLED')
      const mono = concatFloat32(s.chunks, Math.min(s.frames, s.maxFrames))
      s.chunks = []
      if (mono.length === 0) throw new RecorderError('RECORDING_EMPTY')
      return toWavClip(mono, s.inputRate, maxMs)
    })()
    s.clip.then(
      (blob) => {
        if (sessionRef.current !== s) return
        setState('idle')
        if (auto) {
          setLimitReached(true)
          onAutoStopRef.current?.(blob)
        }
      },
      (err: unknown) => {
        if (sessionRef.current !== s) return
        const code = err instanceof RecorderError ? err.code : 'RECORDER_FAILED'
        if (code === 'RECORDING_CANCELLED') return
        setState('error')
        setError(code)
      },
    )
    return s.clip
  }, [maxMs])

  const cancel = useCallback(() => {
    const s = sessionRef.current
    sessionRef.current = null
    if (s) {
      s.cancelled = true
      release(s)
    }
    setState('idle')
    setError(null)
    setElapsedMs(0)
    setLimitReached(false)
  }, [])

  const start = useCallback(async (): Promise<void> => {
    const current = sessionRef.current
    if (current && !current.released && !current.clip) return // already recording
    if (current) {
      current.cancelled = true
      release(current)
      sessionRef.current = null
    }
    setError(null)
    setElapsedMs(0)
    setLimitReached(false)
    if (!isSupported()) throw fail('VOICE_UNSUPPORTED')

    const s: Session = {
      id: ++counterRef.current,
      ctx: null,
      stream: null,
      source: null,
      node: null,
      sink: null,
      chunks: [],
      frames: 0,
      maxFrames: 0,
      inputRate: 0,
      workletDone: false,
      onWorkletDone: null,
      ticker: null,
      backupTimer: null,
      released: false,
      cancelled: false,
      clip: null,
    }
    sessionRef.current = s
    const live = () => sessionRef.current === s && !s.cancelled

    let ctx: AudioContext
    try {
      // Created first, inside the user's click, so browsers allow it to run.
      ctx = new AudioContext({ latencyHint: 'interactive' })
      s.ctx = ctx
    } catch {
      sessionRef.current = null
      throw fail('VOICE_UNSUPPORTED')
    }
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
    } catch (err) {
      release(s)
      if (!live()) return
      sessionRef.current = null
      throw fail(mediaErrorCode(err))
    }
    s.stream = stream
    if (!live()) return release(s) // cancelled or unmounted while the prompt was open

    try {
      await ctx.audioWorklet.addModule(WORKLET_URL)
      if (!live()) return release(s)
      if (ctx.state === 'suspended') await ctx.resume()
      if (!live()) return release(s)
      s.inputRate = ctx.sampleRate
      s.maxFrames = Math.floor((maxMs * s.inputRate) / 1000)
      s.source = ctx.createMediaStreamSource(stream)
      s.node = new AudioWorkletNode(ctx, WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: 'explicit',
        channelInterpretation: 'speakers',
        processorOptions: { maxFrames: s.maxFrames, chunkSize: 4096 },
      })
      s.sink = ctx.createGain()
      s.sink.gain.value = 0 // keeps the graph pulled without playing the microphone back
      s.node.port.onmessage = (event: MessageEvent) => {
        const msg = event.data as { type?: string; samples?: Float32Array }
        if (msg.type === 'chunk' && msg.samples instanceof Float32Array) {
          if (s.frames < s.maxFrames) {
            s.chunks.push(msg.samples)
            s.frames += msg.samples.length
          }
        } else if (msg.type === 'stopped' || msg.type === 'limit') {
          s.workletDone = true
          s.onWorkletDone?.()
          if (msg.type === 'limit' && live()) void finish(s, true).catch(() => undefined)
        }
      }
      s.source.connect(s.node)
      s.node.connect(s.sink)
      s.sink.connect(ctx.destination)
    } catch {
      release(s)
      if (!live()) return
      sessionRef.current = null
      throw fail('RECORDER_FAILED')
    }

    // The device was unplugged or the browser revoked access: keep what was captured.
    stream.getAudioTracks().forEach((track) => {
      track.onended = () => {
        if (live() && !s.clip) void finish(s, true).catch(() => undefined)
      }
    })
    s.ticker = window.setInterval(() => {
      if (live() && s.inputRate > 0) {
        setElapsedMs(Math.min(maxMs, Math.round((s.frames * 1000) / s.inputRate)))
      }
    }, 200)
    // Backup in case the worklet never reports the limit (e.g. a suspended context).
    s.backupTimer = window.setTimeout(() => {
      if (live() && !s.clip) void finish(s, true).catch(() => undefined)
    }, maxMs + 1500)
    setState('recording')
  }, [fail, finish, maxMs])

  const stop = useCallback(async (): Promise<Blob> => {
    const s = sessionRef.current
    if (!s || s.cancelled || (s.released && !s.clip)) throw new RecorderError('NOT_RECORDING')
    try {
      return await finish(s, false)
    } finally {
      if (sessionRef.current === s) {
        sessionRef.current = null
        setElapsedMs(0)
      }
    }
  }, [finish])

  // Release the microphone if the component goes away mid-recording.
  useEffect(
    () => () => {
      const s = sessionRef.current
      sessionRef.current = null
      if (s) {
        s.cancelled = true
        release(s)
      }
    },
    [],
  )

  return { state, error, start, stop, cancel, elapsedMs, maxMs, limitReached }
}
