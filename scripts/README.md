# scripts

Live smoke checks against real external services. They read credentials from `.env` at runtime and never print secret values.

| Command | Checks |
|---|---|
| `make smoke-llm` | Extractor (TokenHub `hy3`) and validator (DeepSeek `deepseek-flash`) return valid JSON |
| `make smoke-asr` | TRTC sentence ASR transcribes a synthetic 16 kHz WAV from macOS `say` |
| `make smoke-langfuse` | One trace with a span and a generation reaches Langfuse Cloud |
| `uv run python scripts/smoke_telemetry.py` | T-08 `shared.telemetry` end to end: a 6-observation trace (spans, a generation, blocked/error outcomes, a second service continued from `traceparent`) with canary secrets in inputs/outputs/attributes/exceptions; flushes, reads it back via `GET /api/public/v2/observations` and asserts structure and that no canary reached Langfuse |
| `uv run python scripts/smoke_extract.py` | T-09 agent extract step only (live `hy3`, fixed `prompts/extractor.md`, no gateway) on 4 synthetic conversations: prints parsed JSON, latency, usage and whether every quote is found verbatim in a user turn |
| `uv run python scripts/smoke_validate.py` | T-10 validator end to end (client, HTTP app, fixed prompt, live model) on 7 synthetic cases: correct draft, wrong amount/recipient, missing leg, injected bill reference, applied correction, ambiguous payee. Verdicts are probabilistic and reported as observed |
| `uv run python scripts/smoke_voice.py` | T-12 gateway voice path: two macOS `say` clips (16 kHz mono 16-bit) through `gateway.asr.validate_wav` and `TrtcTranscriber` against live TRTC; prints transcript, request ID, latency and the SHA-256 of the bytes sent. ASR accuracy is probabilistic and reported as observed |

Demo helper (not a smoke check; no `.env` access): `OPERATOR_TOKEN=... uv run python scripts/operator_console.py list | code <authorization_id> [--base-url http://localhost:8002]` is the SIMULATED step-up phone channel. `list` shows challenges waiting for a code (never codes); `code` "delivers" and prints the six-digit code for one authorization via `POST /operator/step-up/{id}/deliver`. It sends only `X-Operator-Token` from the process environment (never printed), no cookies, no `Authorization` header, and ignores `.netrc`/proxy settings. Not production MFA.

Latest results: `docs/evidence/T-00-smoke.md`.

Local-only demo harness: `uv run python scripts/rogue_agent_demo.py [IDs] [--no-write]` (Rogue Agent Mode attack table; writes `docs/evidence/T-13-attack-matrix.md`). `scripts/operator_console.py` (or `make operator ARGS='code <id>'`) is the simulated phone for step-up codes.
