# docs/evidence

Outputs from real runs, kept for the submission and for review. Nothing here is hand-written to look like a result.

| File | Source |
|---|---|
| `T-00-smoke.md` | Live smoke checks: local build/test, TokenHub + DeepSeek LLMs, TRTC ASR, Langfuse trace |
| `T-04-isolation.txt` | `make isolation-probe`: in-container probes for agent and validator, `.env`-absent proof, image sizes |
| `T-08-telemetry.md` | Telemetry helpers: live Langfuse round trip, canary probe |
| `T-09-extract.md` | Live hy3 extraction smoke, gateway amount/name cross-checks |
| `T-12-voice.md` | Live TRTC transcripts incl. hotword fix for "Ah Ma" |
| `T-13-attack-matrix.md` | Rogue Agent Mode (`scripts/rogue_agent_demo.py`): malicious extractor + validator forced to MATCH vs the real gateway; 46/46 controls held |
| `T-14-results.md` | 35-case golden set: extractor and validator results measured live, Langfuse dataset/experiment links |
| `E2E-live.md` | Full live run over HTTP: draft → clarify → validate → passkey → step-up → commit → receipt, with Langfuse trace |
| `codebuddy/README.md` | CodeBuddy CLI sessions T-00–T-05 compiled from the CLI's own JSONL logs: session IDs, times, turns, tool calls, files, outcome and reviewer fixes; owner steps for the required screenshots |
| `codebuddy/EXCERPTS.md` | Short sanitized excerpts from each CodeBuddy session log (opening, tool calls, test/probe results, credit-exhaustion and network-error endings) |

Secrets are never recorded. CodeBuddy screenshots for the submission go in `codebuddy/screenshots/` when captured (none yet).
