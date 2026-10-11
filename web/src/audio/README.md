# web/src/audio

Push-to-talk voice capture for T-12. The browser records the microphone, encodes a 16 kHz mono 16-bit PCM WAV and uploads it to the gateway. The gateway stores those exact bytes as audio evidence, transcribes them with Tencent TRTC, and appends one `user_asr_final` turn. Speech is only an input channel: the transcript is shown to the user, who can correct it (`POST /api/conversations/{id}/transcript-edit`) before anything is drafted or approved. Typing always remains available.

**Trust boundary.** This code runs in the user's browser and is untrusted. It holds no TRTC credentials and never calls TRTC; it never sends a transcript. The gateway re-checks the upload (RIFF/WAVE header, PCM 16-bit, mono, 16 kHz, ≤ 60 s, ≤ 3 MB, `consent=true`) and produces the transcript itself.

| File | Purpose |
|---|---|
| `useRecorder.ts` | `useRecorder(options?)` → `{ state: 'idle' \| 'recording' \| 'processing' \| 'error', error, start(), stop(): Promise<Blob>, cancel(), elapsedMs, maxMs, limitReached }`. Uses `getUserMedia` and an AudioWorklet; stops by itself at 60 s (`onAutoStop(blob)`, then `stop()` returns the same clip). `stop`, `cancel`, the 60 s limit and unmount release the microphone: every track stopped, nodes disconnected, `AudioContext` closed. `error` is a stable code: `MIC_PERMISSION_DENIED` (NotAllowedError), `MIC_NOT_FOUND`, `MIC_IN_USE`, `VOICE_UNSUPPORTED` (no secure context / AudioWorklet), `RECORDER_FAILED`, `RECORDING_EMPTY`. `start()` also rejects with a `RecorderError` carrying that code |
| `wav.ts` | `downsample` (box-average down, linear up), `floatTo16BitPCM`, `encodeWav` (44-byte header, PCM format 1), `toWavClip` (resample to 16 kHz and cap at 60 s) |
| `api.ts` | `uploadVoice(conversationId, blob)` → `VoiceResponse` (`audio_id`, `digest`, `transcript`, `turn`, `asr: { request_id, latency_ms, audio_duration_ms, engine, language }`, …). Multipart `audio` + `consent=true`. Errors are `ApiError` with the gateway code |
| `messages.ts` | `VOICE_CODE_MESSAGES` / `describeVoiceCode`: plain-English text for every recorder and gateway voice code |
| `../../public/audio-recorder-worklet.js` | The `dcta-recorder` AudioWorkletProcessor: mono mixdown at the context rate, 4096-sample chunks to the main thread, stops at `maxFrames` |

**Gateway codes** (`POST /api/conversations/{id}/voice`): 400 `AUDIO_CONSENT_REQUIRED`, `AUDIO_MISSING`, `AUDIO_EMPTY`; 413 `AUDIO_TOO_LARGE`, `AUDIO_TOO_LONG`, `CONTEXT_LIMIT_RESTATE`; 415 `AUDIO_NOT_WAV`, `AUDIO_NOT_PCM`, `AUDIO_NOT_16BIT`, `AUDIO_NOT_MONO`, `AUDIO_NOT_16KHZ`; 409 `AUDIO_DUPLICATE` (this clip already has its turn); 422 `ASR_NO_SPEECH`; 502 `ASR_UNAVAILABLE`, `ASR_AUTH_FAILED`; 503 `ASR_UNAVAILABLE` (TRTC not configured, nothing stored); 504 `ASR_TIMEOUT`. On ASR failures the recording is kept and no turn is added.

**Consent.** `uploadVoice` always sends `consent=true`; the UI must ask the user once ("Your recording is stored as evidence for 90 days") before the first upload.

**Limitations.** No streaming or interim results (sentence recognition only). Downsampling uses a simple box filter (fine for speech recognition, not hi-fi). A phone needs an HTTPS origin for both the microphone and WebAuthn. No automated browser test: the capture path was type-checked and its encoder output was checked against the gateway's `validate_wav` and live TRTC from Node; a manual browser check (record, deny permission, cancel) is still required.
