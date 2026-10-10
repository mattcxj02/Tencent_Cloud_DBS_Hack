# T-00 smoke results

Date: 9 Oct 2026. **All three live smokes pass.** Scaffold written by CodeBuddy CLI 2.160.0 (headless). Its Bash allowlist did not apply, so it ran nothing; dependency pins, both smoke scripts and the checks below were then fixed and run by the reviewer (Claude Code).

## Local checks

| Check | Result |
|---|---|
| `uv sync --extra dev` | OK |
| `uv run pytest` | 11 passed |
| `uv run ruff check .` | All checks passed |
| `npm install && npm run build` (web) | OK (Vite build, 226 kB JS) |

## Installed versions (locked in `uv.lock`)

fastapi 0.143.0 · uvicorn 0.54.0 · pydantic 2.14.0 · pydantic-settings 2.15.0 · langgraph 1.2.14 · openai 3.26.1 · webauthn 3.0.1 · langfuse 4.17.0 · httpx 0.28.1 · cryptography 50.0.2 · trtc-asr 2.0.0 (git tag; not on PyPI) · pytest 9.1.1 · ruff 0.16.10. Web: React 19, Vite 6, Tailwind 3, @simplewebauthn/browser 12.

## LLM: OK (after key replacement)

First run: the original `tokenhub_key` returned 401 "API Key does not exist" on both TokenHub regions. The user replaced it and added `deepseek_key`.

| Role | Provider / model | Result |
|---|---|---|
| Extractor | TokenHub `hy3` | Valid JSON, finish=stop, 253 completion tokens, 3.2 s |
| Validator | DeepSeek API `deepseek-flash` | Valid JSON, finish=stop, 257 completion tokens, 1.9 s |

Findings:
- TokenHub lists 86 models, but only `hy3` is activated for this account. `deepseek-v4-flash` returns 400 "does not exist"; `deepseek-v4.1-flash`, `deepseek-v4-flash-202605`, `deepseek/deepseek-flash` and `hy4-preview` return 402/401006 "service ID does not exist" until they are enabled under Online Inference Service in the console.
- `hy3` is a reasoning model: with `max_tokens=200` the reasoning consumed the budget and `content` was empty (finish=length). Defaults are now 2048 for both roles.
- DeepSeek direct lists `deepseek-flash` and `deepseek-v4-pro`.
- Validator therefore runs on DeepSeek direct for now (`VALIDATE_PROVIDER=deepseek`); switch to TokenHub once a DeepSeek model is activated there.

## TRTC ASR: OK

SDK `trtc_asr.v3.SentenceRecognizer`, host `asr-intl.cloud-rtc.com`, engine `bigmodel`, language `en`, WAV 16 kHz mono 16-bit from macOS `say` (59,108 bytes, 1,719 ms).
Input phrase "send fifty dollars to John" → transcript **"Send fifty dollars to John."** (request_id `5ed90438-c1d8-4a39-a5d0-302489bd7810`).

## Langfuse: OK

Langfuse Cloud US region, SDK 4.17.0, `auth_check()` OK. Trace `ae94ccbc9427f7bab35270fd65ba1686` exported and read back through `GET /api/public/v2/observations`: SPAN `dcta-smoke-test` → SPAN `extract` → GENERATION `extractor-llm-call`, all with session `smoke-test-session`.
Trace URL: https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/traces/ae94ccbc9427f7bab35270fd65ba1686

Note: this org is new, so legacy read APIs (`GET /api/public/traces/{id}`) return 410; use the v2 observations API.
