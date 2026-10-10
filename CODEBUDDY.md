# CODEBUDDY.md — DCTA project rules

Project: DBS track, Scenario 1, Direct Conversational Transaction Agent. Read `docs/PLAN.md`, `docs/ARCHITECTURE.md` and `docs/instructions.md`. v0.3 retains the full MVP at the user's request. **Langfuse is a required real observability backend**, not an optional exporter or console-log substitute. All implementation tasks remain pending until their checks actually pass.

**Scope and authority**

Keep SGD transfers (own account/saved payee/saved PayNow), bill payments, card payments, mock SGX equities, 1–4-leg bundles, dependencies, voice/text, clarification/corrections, WebAuthn, mandates/cooling/pause, KYC/risk/scam controls, simulated secondary approval, audit/evidence UI and evaluations. Tencent APM, TTS and public hosting are optional. `docs/PLAN_REVIEW.md` contains recommendations, including scope cuts the user has not adopted. Do not implement those cuts or silently replace real WebAuthn with a mock. Report schedule/integration blockers honestly.

GenAI generates drafts and fallible semantic assessments; it never authorizes funds. Model calls exist only in agent extraction and the separate read-only validator. The workflow is fixed by code; models do not select tools. Treat either or both models returning a malicious MATCH as an expected test condition. Never describe a prompt as injection-proof or a model result as a signature.

**Invariants — enforce with meaningful checks**

- I1: agent and validator have no ledger mount/write access, execution dispatcher or signing credentials. Forbid imports of gateway/ledger in both; check transitive shared-code boundaries and actual runtime permissions.
- I2: execution requires valid user approval bound to the exact gateway-stored signing envelope, operation and authenticated owner. Verify source ownership and credential/session binding, not just signature validity.
- I3: confirmation renders that same gateway envelope. Edits create a new revision and supersede validation and approval.
- I4: approvals and secondary proofs are fresh/single-use. Concurrent/repeated requests cause at most one commit; committed retries return the stored receipt.
- I5: hard mandate, KYC, sanctions, ownership, funds and velocity blocks cannot be overridden by model output or secondary approval. Keep old mandate active during cooling; decreases/pauses cancel pending increases.
- I6: required audit events commit with financial effects. Workflow stages have correlated Langfuse observations; live backend traces/experiments are required acceptance evidence.
- I7: trusted code assigns source lineage and preserves derived dependencies/taint. Untrusted references cannot become critical fields via relabeling; valid span references do not prove semantic correctness.
- I8: semantic uncertainty, errors and malformed output prevent approval. The agent cannot supply its own verdict. Even a validator forced to MATCH cannot bypass I1–I5.
- I9: ordered mock bundles are all-or-nothing. Approved equity quantities, price constraints and debit remain fixed; expiry/changes require a new draft and approval.

**Contracts**

Use `SHA256(b"DCTA:approval:v1\x00" + canonical_bytes(SigningEnvelope))` in one shared helper for both challenge issuance and verification. Envelope contains schema version, operation, user/credential, operation ID, frozen full payload, conversation binding where applicable, active mandate version, nonce and timestamps. Gateway stores the bytes; execute accepts authorization ID, expected hash and assertion, never a replacement payment payload. Use distinct execute_bundle/update_mandate operations. TTL <=120 seconds and no later than quote expiry.

Gateway captures original authenticated conversation turns and corrections; agent credentials cannot fabricate user turns. Gateway invokes validator directly with full ordered history, frozen bundle and bank mappings. Exclude extractor reasoning. Validator prompt/model/schema are backend-controlled, versioned and recorded; never accept system instructions from client fields or expose secrets in prompts. Bind results to bundle/conversation hashes and revisions. No live runtime bypass flags for tests.

Execution and secondary approval use the state machine in ARCHITECTURE. Atomically recheck policy/state and commit effects, nonce/proof consumption, counters, receipt and audit. Recheck immediately after step-up as well. Idempotency uses user+operation+operation ID with immutable payload hash. Validation is required for conversational bundles; direct signed mandate settings use deterministic validation. Invalid signatures never become approvals.

**Stack, files and permissions**

Python 3.12/uv; FastAPI, Pydantic/settings, LangGraph, provider-compatible client, webauthn, Langfuse, httpx, cryptography; pytest/ruff. SQLite belongs to gateway. React/Vite/TypeScript/Tailwind and SimpleWebAuthn browser helper. Pin tested SDK versions; the prior Langfuse v3 assumption is superseded by current official docs. Check account-level model/ASR/backend access instead of assuming trial credits.

```
agent/        fixed workflow, extraction and READ/DRAFT API clients
validator/    internal semantic audit service without payment tools
gateway/      identity, captured conversation, frozen drafts, validation coordinator,
              policy/mandates, approval/step-up, execution and audit
ledger/       gateway-only mock bank models and seed
shared/       pure types/canonicalization, safe telemetry and scoped HTTP clients
prompts/      version-controlled instructions
web/          chat/audio, confirmation, mandate settings, evidence viewer
infra/        isolated service images/Compose
evals/        versioned datasets, Langfuse experiment runner and reports
tests/        contracts, model fixtures, gateway, isolation, lifecycle and attacks
```

Only gateway has the ledger/evidence mount and identity/secondary-approval secrets. Agent/validator receive narrowly scoped service credentials and their own model keys. Internal routes check service scope and owner/session binding. Shared utilities must not smuggle execution capabilities across the boundary. Never mount a Docker socket or full host workspace into the agent runtime.

**Langfuse and audit handling**

Use a configured Langfuse project with real generation/workflow traces and evaluation experiments. No-op exporters are test fixtures only. Share trace context across services, link resumed turns through pseudonymous session/revision IDs, and persist trace IDs in audit. Log prompt version/hash, model, safe input/output, latency and available usage/cost; missing cost is unavailable, not zero. Do not leave spans open during human waiting.

Allowlist/redact before export, including errors. Never send API keys, auth headers/cookies, OTPs, assertions, private keys, raw audio or full account IDs to Langfuse. Disable indiscriminate argument capture. Keep raw evidence access-controlled locally and use synthetic model-visible banking data. Langfuse outage must show degraded health and bounded export retry without changing authorization or re-executing payments. Local mandatory audit failure blocks commit. Do not claim the local hash chain is regulatory-grade or protected against a privileged complete rewrite.

**Implementation discipline**

- Decimal for money; serialize two-decimal strings. SQLite integer minor units or validated decimal strings, never REAL. Reject float/non-finite values and unsupported precision.
- UTC timestamps in storage/signing; Asia/Singapore calendar days for daily policy.
- Pydantic schemas at HTTP/model boundaries; type hints; small readable functions. No arbitrary expression evaluation, model HTML or model-selected tools.
- Schema-valid model output can still be false. Semantic checks and deterministic authorization are tested/reported separately.
- Tests cover meaningful financial, authorization, lifecycle and privacy behavior. Documentation-only changes use consistency/link checks; do not invent a test run when no app exists.
- For code changes run relevant tests, `uv run ruff check .` and frontend build when affected. Run required integration checks and report unavailable live credentials separately from passing local tests.
- Every project folder has a `README.md` stating its purpose, its files and its trust boundary. When you add, rename or change the role of a module or folder, update that folder's README in the same task.
- Do not add dependencies without explaining their purpose. Consult installed versions/current official docs when implementing SDK integration.
- Never create/edit `.env`, print credentials, modify references, commit, push or modify git configuration. Update `.env.example` with placeholders. Publishing/submitting requires the user's instruction.
- Use CodeBuddy for meaningful tasks and capture at least three sanitized development screenshots/recordings. Do not fabricate tool-usage evidence, metrics or completed task status.

Finish with files changed, checks and results, and unfinished work or limitations. Scope reductions in the review remain optional until explicitly adopted by the user.
