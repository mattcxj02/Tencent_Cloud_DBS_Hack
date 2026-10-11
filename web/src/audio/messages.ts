// Plain-English text for every voice code: the recorder's own codes and the gateway's
// POST /api/conversations/{id}/voice codes. Spread into the app's code table
// (`{ ...CODE_MESSAGES, ...VOICE_CODE_MESSAGES }`) or call describeVoiceCode directly.
// Every message keeps the text path open: typing always works.

export const VOICE_CODE_MESSAGES: Record<string, string> = {
  // Browser capture (useRecorder)
  MIC_PERMISSION_DENIED:
    'Microphone access was blocked. Allow the microphone for this site in your browser settings, or type your request instead.',
  MIC_NOT_FOUND: 'No microphone was found. Connect one, or type your request instead.',
  MIC_IN_USE: 'The microphone is busy or could not start. Close other apps using it, or type instead.',
  VOICE_UNSUPPORTED:
    'This browser cannot record here (it needs a secure page and AudioWorklet support). Please type instead.',
  RECORDER_FAILED: 'Recording could not start. Please try again or type instead.',
  RECORDING_EMPTY: 'Nothing was recorded. Hold the button a little longer, or type instead.',
  NOT_RECORDING: 'There is no recording in progress.',
  RECORDING_CANCELLED: 'Recording cancelled.',

  // Gateway upload checks (nothing stored, speech service not called)
  AUDIO_CONSENT_REQUIRED: 'Please agree to the recording being stored before using voice.',
  AUDIO_MISSING: 'No recording was sent. Please try again.',
  AUDIO_EMPTY: 'The recording was empty. Please try again.',
  AUDIO_TOO_LARGE: 'The recording is too large. Please keep it under a minute.',
  AUDIO_TOO_LONG: 'The recording is longer than one minute. Please keep it shorter.',
  AUDIO_NOT_WAV: 'The recording format was not accepted. Please try again.',
  AUDIO_NOT_PCM: 'The recording format was not accepted. Please try again.',
  AUDIO_NOT_16BIT: 'The recording format was not accepted. Please try again.',
  AUDIO_NOT_MONO: 'The recording format was not accepted. Please try again.',
  AUDIO_NOT_16KHZ: 'The recording format was not accepted. Please try again.',
  AUDIO_DUPLICATE: 'This recording was already added to the conversation.',
  AUDIO_STORE_FAILED: 'The recording could not be saved. Please type your request instead.',

  // Speech recognition (the recording is kept; no text was added)
  ASR_UNAVAILABLE: 'Speech recognition is unavailable right now. Please type your request instead.',
  ASR_AUTH_FAILED: 'Speech recognition is not set up correctly. Please type your request instead.',
  ASR_TIMEOUT: 'Speech recognition took too long. Please try again or type instead.',
  ASR_NO_SPEECH: "We couldn't make out any words. Please try again or type instead.",
  CONTEXT_LIMIT_RESTATE:
    'This conversation is too long. Please start again and restate your whole request.',
}

/** Text for a voice code, or null when the code is not a voice code. */
export function describeVoiceCode(code: string | null | undefined): string | null {
  if (!code) return null
  return VOICE_CODE_MESSAGES[code] ?? null
}
