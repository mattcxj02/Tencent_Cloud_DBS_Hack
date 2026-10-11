# T-12 voice evidence

Date: 10 Oct 2026. Gateway voice path (`gateway/asr.py`, `gateway/asr_routes.py`) and browser capture (`web/src/audio/`), built by a Claude subagent and reviewed by the lead.

## Live TRTC (`make smoke-voice`, re-run by the lead)

| Said (macOS `say`, 16 kHz mono WAV) | Transcript | Latency |
|---|---|---|
| "Send fifty dollars to Mum from my savings" | "Send fifty dollars to Mum from my savings." | 493 ms |
| "Pay Ah Ma one hundred dollars" (no hotwords) | "Pay **Amma** one hundred dollars." | 274 ms |
| same clip, with the user's saved payee names as TRTC hotwords | "Pay **Ah Ma** one hundred dollars." | 260 ms |

The lead added hotwords: the voice route passes the user's own saved payee names, aliases and saved billers as TRTC `hotword_list` (sanitized, ≤30 words). Without them "Ah Ma" is misheard and the agent would have to ask.

Subagent checks (not re-run by the lead): a 48 kHz browser-style clip through `web/src/audio/wav.ts` transcribed correctly in 438 ms; the real `/voice` route on a temp DB with real TRTC stored the clip, sent exactly the stored bytes to TRTC (matching sha256), appended a `user_asr_final` turn and verified the audit chain; its Langfuse trace (https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/traces/f48de204461ac2172911ecffb3b20eb7) contains no transcript, audio or secrets.

## Rules

16-bit PCM mono 16 kHz WAV only (parsed server-side, not trusted from the content type), ≤3 MB, ≤60 s, consent required; the audio is stored before transcription; an ASR failure keeps the audio and appends no turn; one clip gives at most one turn; the old client-supplied `/asr` route is removed.

## Tests

`tests/test_t12_voice.py`: 49 passed.

Pending: manual browser check of microphone capture (record, deny permission, cancel, 60 s auto-stop) with a human voice.
