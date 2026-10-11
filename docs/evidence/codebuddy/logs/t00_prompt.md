You are implementing task T-00 of this repository. First read CODEBUDDY.md, then the T-00 section of docs/instructions.md, and the "Stack and layout" section of docs/PLAN.md. Follow them exactly. Do only T-00; do not start T-01.

Hard rules (repeat of CODEBUDDY.md, non-negotiable):
- NEVER read, print, cat, grep, edit or create `.env`. Code may load it at runtime via pydantic-settings; you must not inspect it. Never print secret values in logs or output.
- Do not commit, push or change git config. Do not modify `references/`.
- Do not create docs/PLAN.md changes. You may update docs/STATUS.md only to set T-00's row and add a short note under it.

Facts you need (already verified by the reviewer):
- The user's existing `.env` uses exactly these variable names. Map settings to them with pydantic-settings aliases; do NOT require the user to rename anything:
  - `tokenhub_key` — TokenHub API key (OpenAI-compatible). Base URL default: `https://tokenhub-intl.tencentcloudmaas.com/v1` (non-secret, configurable as `LLM_BASE_URL`).
  - `SDKAppID`, `SDKSecretKey` — TRTC ASR application credentials (gateway only). Host default `asr-intl.cloud-rtc.com` (configurable `TRTC_ASR_HOST`). These are NOT the cloud API SecretId/SecretKey.
  - `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` — Langfuse Cloud.
  - Non-secret defaults (model IDs, ports, policy numbers) belong in settings defaults and `.env.example`, not in `.env`.
- Candidate models: extractor `hy3`, validator `deepseek-v4-flash`. These IDs are UNVERIFIED (a nearby name `deepseek-v4.1-flash` exists). The model smoke must first call `GET /models` on TokenHub and print the available model IDs, then test structured JSON output on the configured extractor and validator IDs. Report honestly which IDs work.
- TRTC ASR Python SDK: `pip install trtc-asr` (import `trtc_asr`; use `trtc_asr.v3` SentenceRecognizer, engine `bigmodel`, language `en`; audio wav/pcm 16 kHz mono, <=60 s). Read the installed package source/examples to get the exact API instead of guessing. For the smoke, generate a short synthetic 16 kHz mono 16-bit WAV of English speech with macOS `say -o <file>.wav --data-format=LEI16@16000 "send fifty dollars to John"` (keep generated audio under a gitignored path), send it, and print the transcript.
- Langfuse: install the current Python SDK, check its installed version and use the API matching that version (read the package or official docs; do not mix v2/v3 examples). The smoke creates one trace with one span and one generation-type observation using synthetic data, flushes, and prints the trace ID/URL (no keys).
- Tool versions available: Python 3.12.6, uv 0.9.15, node 23.6, npm 11.4. Docker daemon is NOT running: do not run docker commands; you may write `infra/` Compose files only if trivial, otherwise leave T-04 for later.
- Ports: agent 8001, gateway 8002, validator 8003. The browser talks ONLY to the gateway (`/api/gateway` proxy in Vite). Agent and validator are internal.

Deliverables:
1. uv project (Python 3.12) with packages agent, validator, gateway, ledger, shared, plus tests/, evals/, prompts/, infra/. Each service is a FastAPI app with GET /health. Per-service settings classes that only load the secrets that service needs (gateway: TRTC + Langfuse; agent: LLM + Langfuse; validator: LLM + Langfuse).
2. web/: React + Vite + TypeScript + Tailwind with chat/confirmation placeholder components and `@simplewebauthn/browser` dependency; proxy `/api/gateway` → 8002.
3. Makefile targets: test, lint, web-build, dev (start the three services), seed (placeholder that says pending T-02), demo (placeholder), eval (placeholder), smoke-llm, smoke-asr, smoke-langfuse.
4. `.env.example` with the exact names above and placeholder values only. Extend `.gitignore` (keep the existing `.env` line) for .venv, __pycache__, node_modules, dist, data/, recordings/, *.db, .ruff_cache, .pytest_cache, generated smoke audio.
5. Health tests with pytest; `uv run ruff check .` clean; `npm run build` in web passes.
6. Run all three smoke commands. They read credentials at runtime only. Redact anything secret-looking from output. Write results to `docs/evidence/T-00-smoke.md`: date, exact SDK versions installed (`uv pip list` subset), provider/model IDs that worked or failed (with error class, no secrets), ASR transcript, Langfuse trace ID/URL. If a smoke fails, record the real failure; never mark it passed.

Finish with: files changed, checks run and their results, smoke results, and remaining limitations/blockers.
