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

Latest results: `docs/evidence/T-00-smoke.md`.
