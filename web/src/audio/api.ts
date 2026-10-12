// Upload one push-to-talk clip: POST /api/conversations/{id}/voice (multipart).
// The gateway stores these exact bytes as audio evidence, transcribes them with Tencent
// TRTC and appends one `user_asr_final` turn. The transcript is shown to the user, who can
// correct it (POST .../transcript-edit) before anything is drafted or approved.
// `consent=true` is always sent: the UI must ask the user once before the first upload.
import { request, type Turn } from '../api'

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
export async function uploadVoice(conversationId: string, blob: Blob, operationId?: string, signal?: AbortSignal): Promise<VoiceResponse> {
  const wav = blob.type === 'audio/wav' ? blob : new Blob([blob], { type: 'audio/wav' })
  const form = new FormData()
  form.append('audio', wav, 'clip.wav')
  form.append('consent', 'true')
  const query = operationId ? `?operation_id=${encodeURIComponent(operationId)}` : ''
  return request<VoiceResponse>('POST', `/conversations/${encodeURIComponent(conversationId)}/voice${query}`, {
    form, signal, timeoutMs: 35000,
  })
}
