// Upload one push-to-talk clip: POST /api/conversations/{id}/voice (multipart).
// The gateway stores these exact bytes as audio evidence, transcribes them with Tencent
// TRTC and appends one `user_asr_final` turn. The transcript is shown to the user, who can
// correct it (POST .../transcript-edit) before anything is drafted or approved.
// `consent=true` is always sent: the UI must ask the user once before the first upload.
import { ApiError, type Turn } from '../api'

export interface VoiceAsrInfo {
  /** TRTC request ID (correlation only). */
  request_id: string
  latency_ms: number
  /** Duration TRTC reports for the clip. */
  audio_duration_ms: number
  engine: string
  language: string
}

export interface VoiceResponse {
  conversation_id: string
  audio_id: string
  /** SHA-256 of the stored WAV bytes (the same bytes sent to TRTC). */
  digest: string
  size_bytes: number
  /** Duration parsed from the WAV header by the gateway. */
  duration_ms: number
  consent: boolean
  retention_until: string
  transcript: string
  /** The appended `user_asr_final` turn (null only if the gateway appended none). */
  turn: Turn | null
  asr: VoiceAsrInfo
  trace_id: string | null
}

/**
 * Upload a WAV Blob from useRecorder. Resolves with the transcript and its turn.
 * Rejects with ApiError whose `code` is a gateway voice code (see ./messages.ts), e.g.
 * ASR_NO_SPEECH, ASR_TIMEOUT, AUDIO_TOO_LONG, or SESSION_REQUIRED / NETWORK_ERROR.
 * On ASR failures the recording is kept; `error.body.detail.audio_id` names it.
 */
export async function uploadVoice(conversationId: string, blob: Blob): Promise<VoiceResponse> {
  const wav = blob.type === 'audio/wav' ? blob : new Blob([blob], { type: 'audio/wav' })
  const form = new FormData()
  form.append('audio', wav, 'clip.wav')
  form.append('consent', 'true')
  let res: Response
  try {
    res = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/voice`, {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch {
    throw new ApiError(0, 'NETWORK_ERROR', [], null)
  }
  const data: unknown = await res.json().catch(() => null)
  if (!res.ok) {
    const body =
      data !== null && typeof data === 'object' && !Array.isArray(data)
        ? (data as Record<string, unknown>)
        : null
    throw new ApiError(res.status, voiceErrorCode(res.status, body), [], body)
  }
  return data as VoiceResponse
}

function voiceErrorCode(status: number, body: Record<string, unknown> | null): string {
  if (status === 401) return 'SESSION_REQUIRED'
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (detail !== null && typeof detail === 'object' && !Array.isArray(detail)) {
    const code = (detail as Record<string, unknown>).code
    if (typeof code === 'string') return code
  }
  if (Array.isArray(detail)) return 'INVALID_REQUEST'
  return `HTTP_${status}`
}
