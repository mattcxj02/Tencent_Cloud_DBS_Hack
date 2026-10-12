# gateway

The trusted, deterministic core (FastAPI, port 8002). It is the only service the browser talks to and the only one that can open the ledger database. It owns identity, the conversation record, frozen drafts, approval, hard policy, atomic execution and the audit chain; validation coordination (T-10) and secondary approval (T-07) plug into the same hooks.

| File | Purpose |
|---|---|
| `app.py` | FastAPI app, CORS limited to the WebAuthn origin; `GET /health` and `/api/health` (credential status plus `telemetry`: Langfuse export health `ok` / `degraded` / `disabled`, no secrets) |
| `bank.py`, `bank_models.py`, `bank_routes.py` | User-scoped read queries and the `/internal/bank/*` API for the agent (scope `bank:read`; other users' IDs return 404); mock SGX quotes expire after 5 minutes; transaction history includes `direction` (`debit`/`credit`) |
| `conversations.py`, `conv_models.py`, `conv_routes.py` | Append-only turns, revisions, conversation hash, size limits, audio evidence; user routes `/api/conversations/*`; internal snapshot, question and agent-event routes. No route accepts a client-supplied ASR transcript |
| `clarifications.py` | Grouped question candidates, gateway-owned labels/choice IDs, authenticated selections and extraction checkpoints in the mandatory local audit log; no schema upgrade required |
| `asr.py`, `asr_routes.py` | T-12 voice. `asr.py`: WAV contract check (`validate_wav`: RIFF/WAVE, PCM 16-bit, mono, 16 kHz, ≤ 60 s, ≤ 3 MB; bytes parsed, content type not trusted), `TrtcTranscriber` (TRTC `trtc_asr.v3.SentenceRecognizer`, host `TRTC_ASR_HOST`, engine `bigmodel`, language `en`, 20 s socket timeout; errors → `ASR_UNAVAILABLE` / `ASR_TIMEOUT` / `ASR_AUTH_FAILED`, SDK messages dropped) and `append_voice_turn` (one transaction: the clip's single `user_asr_final` turn + `audio_evidence.turn_id` link + `gateway.voice.transcribed` audit; a clip, or the same bytes in that conversation, never gets a second turn). `asr_routes.py`: `POST /api/conversations/{id}/voice` (sync; session owner, Origin, multipart `audio` + `consent=true`): store the exact bytes, re-read and digest-check them, send them to TRTC, append the turn; ASR failure keeps the audio, appends nothing (502/504, 422 `ASR_NO_SPEECH`); `GET .../voice` (clip metadata) and `GET .../voice/{audio_id}` (stored WAV, digest re-checked, `no-store`, 410 after retention). `ingest` span with channel, TRTC request ID, latency and clip duration only — never audio or transcript. Live check: `scripts/smoke_voice.py` |
| `identity.py`, `identity_routes.py`, `cli_enroll.py` | Passkey enrollment (`make enroll-code`: 15-minute expiry, at most five begins, code consumed only with successful registration; code-bound sibling challenges invalidated atomically on success), login, server-side sessions, logout, Origin (CSRF) checks; `GET /api/auth/me` → `{user_id, display_name, credentials: [{credential_id, created_at}]}` (no keys or sign counts) |
| `drafts.py`, `draft_routes.py` | Freeze untrusted agent proposals into immutable bundles (`POST /internal/drafts`, scope `draft:freeze`); issue the signing envelope (`POST /api/drafts/{id}/authorizations`); show it (`GET /api/authorizations/{id}`); approve and commit (`POST /api/authorizations/{id}/approve`); fetch the stored receipt (`GET /api/authorizations/{id}/receipt`) |
| `policy.py` | Deterministic hard policy `evaluate(conn, envelope, now)` → `PASS` / `STEP_UP` / `BLOCK` with stable reason codes (`REASON_CODES`) |
| `scam_signals.py` | Deterministic scam phrases over the user's own captured turns; escalate to step-up only (rules and false positive/negative limits in the module docstring) |
| `execution.py` | `commit(db, authz, now, step_up_proof=None)`: the only code that moves (simulated) money — recheck and apply every leg in one `BEGIN IMMEDIATE` transaction; an `AWAITING_STEP_UP` authorization needs a step-up proof, which runs and is consumed under the same lock |
| `stepup.py`, `stepup_routes.py` | T-07 **simulated** out-of-band step-up: one challenge per authorization entering `AWAITING_STEP_UP`, bound to user, authorization, envelope hash and expiry; six-digit code derived with a gateway secret (`STEP_UP_SECRET`, else per-process random) and never stored — only a salt and keyed HMAC verifier; at most 5 failed attempts (the fifth → `BLOCKED`); single use; resend never extends expiry. User routes `POST/GET /api/authorizations/{id}/step-up`, `POST .../step-up/resend`, `POST /api/authorizations/{id}/cancel` |
| `operator_routes.py` | Demo operator console standing in for "a code sent to your phone": `GET /operator/step-up` (no codes), `POST /operator/step-up/{authorization_id}/deliver` (the code, `Cache-Control: no-store`). Needs `X-Operator-Token` = `OPERATOR_TOKEN` (≥32 chars; unset → 503 disabled); refuses any request carrying a user session cookie or an `Authorization` (agent token) header. Responses carry a simulation label; not production MFA |
| `mandates.py`, `mandate_routes.py` | T-07 signed mandate management: `GET /api/mandate`, `POST /api/mandate/changes` (→ `update_mandate` envelope + WebAuthn options), `GET /api/mandate/changes/{id}`, `POST /api/mandate/changes/{id}/approve`. Deterministic change rules; tightening (decrease/removed action/pause) applies at once, cancels the pending increase and supersedes pending approvals and their step-up challenges; loosening (increase/added action/unpause) waits `COOLING_HOURS` (12) as the user's one pending change, activated lazily under the write lock (`activate_due_changes`); mixed requests are rejected. Operation-specific policy `evaluate_change` (no funds/velocity/KYC/semantic checks; paused or expired mandates stay manageable) |
| `clock.py` | `utcnow()`, the gateway's single injectable clock (tests use `_set_clock`; no HTTP or env switch) |
| `calculations.py` | Freezes exact derived-amount inputs in the same transaction as the bundle (balance snapshot, ordered actual debits, dependency amounts, result); reads only those frozen inputs and refuses changed source balances. They are included in validator input hashes and rechecked at result completion, issuance, approval and commit |
| `evidence.py` | Checks the agent's citations of user words and builds trusted lineage: words must be whole words in a current user turn (not a fragment such as "Mum" in "Mumbai") and name exactly one bank record; amounts cannot come from bill references |
| `audit.py` | Hash-chained audit log, verification, and supersession of records bound to an old conversation revision. Gateway rows appended without explicit IDs take the trace/span IDs of the active gateway workflow span (agent rows keep the IDs the agent reported) |
| `telemetry.py` | T-08 gateway telemetry: one `shared.telemetry.Telemetry` per process from `GatewaySettings` (disabled without Langfuse keys), `stage(...)` workflow spans (pseudonymous user, session = conversation), `record_policy`, `health()`, owner trace links (`trace_urls`, one project lookup, failures cached 60 s). Tests inject a facade with `_set_gateway_telemetry` (not reachable over HTTP); validation and voice use the same facade |
| `orchestration.py`, `orchestration_routes.py`, `drafting_work.py` | Owner/revision-scoped `POST /api/conversations/{id}/draft`: single-flight admission, bounded readiness, agent extraction and independent validation within `DRAFT_TIMEOUT_SECONDS` (default 90 s). Delegation tokens are bound to the active run; late callbacks and superseding input cannot publish a question/draft or start validation. Returns gateway-held clarification, ready or correction views; stable errors otherwise. A transient validation retry reuses only unchanged frozen work and stored MATCH uses existing eligibility checks |
| `read_routes.py`, `evidence_view.py` | `GET /api/accounts` → `[{account_id, display, balance}]` (masked display). `GET /api/conversations/{id}/evidence` (owner only): turns (corrected originals flagged `superseded`), drafts with legs and latest validation (+ trace link), payment authorizations with approval evidence (credential ID, time, `user_verified`), step-up status/attempts and receipt, audit events without payloads plus the chain check, and trace links. Never audit payloads, envelope bytes/nonces, assertion data or hashes, step-up codes/salts/verifiers, tokens or audio; text is raw for the UI to escape |
| `payees.py`, `payee_routes.py` | Payee names for matching: display name, suffix, bank aliases (`payee_aliases`) and customer nicknames (`payee_nicknames`), read by evidence, validator labels, payee search and ASR hotwords. `GET /api/payees`, `POST /api/payees/{id}/nicknames`, `DELETE /api/payees/{id}/nicknames/{nickname}` (session + Origin, audited). A nickname that would name any other payee, account, biller, card or stock is refused (`NICKNAME_CONFLICT`). Payee search returns exact matches, then near spellings marked `match: "fuzzy"` (suggestions only; never evidence) |
| `auth/` | Delegation tokens for the agent and the browser-session dependency (see `auth/README.md`) |
| `validation.py`, `validation_routes.py` | T-10 semantic validation coordinator: builds the validator request from gateway records only (stored bundle bytes, the turns covered by the stored conversation hash without timestamps, gateway-resolved bank labels incl. same-name candidates), calls the internal validator, stores the result in `validation_results` bound to bundle/conversation/bank-label/input hashes with prompt hash, model and trace ID, and audits it (`gateway.validation.*`). `POST /api/drafts/{id}/validate` (session, owner, Origin; body must be empty) and `GET /api/drafts/{id}/validation`. MATCH → eligible; MISMATCH → draft `VALIDATION_MISMATCH` (never issued); UNCERTAIN → clarify; ERROR → at most 3 attempts per draft, rate limited, never MATCH. `semantic_validation_passed` is the default eligibility hook |
| `approval/` | Eligibility hooks: semantic validation (bound MATCH from `validation.py`), policy pre-check and commit (see `approval/README.md`) |

**Structured clarification:** the internal question route accepts a revision, per-leg
target/source candidates and an untrusted extraction checkpoint. The gateway resolves
owned records and issues its own field and choice IDs. The session/Origin-protected
structured answer contains no client text: it must select exactly one choice for every
field of the latest question at its revision. The gateway generates the answer words
from current bank labels and atomically stores the turn, revision, supersession and
selection audit. Freeze independently checks the exact span, field, leg, record,
ownership and revision; a selection cannot supply an amount or another leg's field.
Question/selection metadata is stored in existing gateway audit rows, so no database
upgrade or new setting is needed. Subsequent user input invalidates the extraction
checkpoint and selection evidence; free text and corrections run extraction and may
require reconfirming previously selected fields. Final semantic validation still runs.

**Approval rules:** issuance requires a current `DRAFT_FROZEN` draft, the user's own credential, a passed semantic validation and a policy pre-check that is not `BLOCK` (`STEP_UP` may be issued); the operation is always `execute_bundle` and its ID is the bundle ID (mandate changes use their own routes and the distinct `update_mandate` operation; each route refuses the other's authorizations with `OPERATION_MISMATCH`). Approval recomputes the challenge from the stored envelope bytes, verifies the passkey assertion (origin, RP ID, user verification, sign count) and records it once under a write lock. A bad assertion is audited and changes nothing. A correction or a new mandate version supersedes pending approvals. A signature alone never commits. A repeated approve while `AWAITING_STEP_UP` returns that pending status (no second code, no payment).

**Hard policy (`policy.py`):** KYC verified and unexpired with tier daily ceilings 500 / 5,000 / 20,000 SGD; the current mandate (highest version that is `active` or `paused`) must be active, unexpired, allow each action and the leg count (`max_legs`, at most 4), with per-leg and daily limits read from the mandate row. The stricter of the KYC ceiling and mandate daily limit applies. **Every outgoing leg counts toward the daily ceilings, including transfers between the user's own accounts** — a deliberately conservative rule. Days are Asia/Singapore calendar days; stored times are UTC. Source accounts must be held by the user; targets must be the user's own (different) account, saved payee or saved PayNow recipient of the matching kind, saved biller or owned card; sanctioned payees block. Equity legs need an existing, unexpired quote with the frozen symbol, price and expiry. Funds are simulated leg by leg in order (own-account transfers credit the destination for later legs; card payments may not exceed the outstanding balance). Velocity: at most five transfer legs in any rolling ten minutes, counting committed transfers and the candidate bundle. Within all hard limits, a bundle total above `RISK_THRESHOLD` (500.00) or a scam signal gives `STEP_UP`. `BLOCK` always wins and carries only block codes; no model output or step-up proof overrides it. All amounts are prototype defaults.

**Execution (`execution.py`):** under one `BEGIN IMMEDIATE` lock: re-read the authorization for the authenticated owner and rebuild the envelope from stored bytes (hash checked); apply the idempotency key `(user_id, operation, operation_id)` with the stored payload hash (same payload → original receipt, no effects, even after expiry; different payload → `IDEMPOTENCY_CONFLICT`); require `PENDING_APPROVAL` (or `AWAITING_STEP_UP` with a step-up proof) and recorded passkey evidence; expire a lapsed approval (`EXPIRED`); verify and consume the step-up code (wrong → attempt counted; fifth → `BLOCKED`); activate a cooled mandate change that is due; supersede on conversation, mandate-version or draft change; re-check semantic validation and the full policy (`BLOCKED` / `AWAITING_STEP_UP` move no money); a step-up that meets `BLOCK` ends `BLOCKED` with the code consumed, never reusable; then write every leg's effect from the frozen envelope (debit, own-account credit, card outstanding, frozen equity quantity and actual debit, simulated settlement reference), the `transactions` rows, the receipt, `COMMITTED`, the draft's `EXECUTED` status and the audit event. Any exception — including an audit write failure — rolls back everything; there are no partial legs.

**HTTP contract:** money-moving routes are plain `def` handlers (FastAPI threadpool) so synchronous SQLite does not block the event loop; concurrent requests serialize on the SQLite write lock (`busy_timeout` 10 s). Approval returns 200 with `status` `COMMITTED` (+ `receipt_id`, `receipt`, `replayed`), `AWAITING_STEP_UP` or `BLOCKED` (+ `reason_codes`). Errors are `{"detail": <stable code>, "reason_codes": [...]}`: 404 `AUTHORIZATION_NOT_FOUND`, 503 `COMMIT_FAILED` / `COMMIT_NOT_IMPLEMENTED` (no state change), 409 for everything else (`POLICY_BLOCK`, `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_SUPERSEDED`, `ALREADY_APPROVED`, `NOT_PENDING: <status>`, `IDEMPOTENCY_CONFLICT`, ...). Clients key on these codes, never on prose. A repeated approve of a committed authorization, or `GET /api/authorizations/{id}/receipt`, returns the stored receipt.

**Semantic validation (`validation.py`):** results live in `validation_results` (`ledger/ddl/validation.sql`) with `state` `PENDING` (call in flight, write lock released) → `CURRENT` (latest finished result; one per bundle) or `SUPERSEDED`. A result stored after the conversation, draft or bundle hash changed mid-call is stored `SUPERSEDED`. Rows are registered in `audit.REVISION_BOUND_TABLES`, whose pending list covers `PENDING`; `CURRENT` rows of older revisions are marked `SUPERSEDED` (audited) on the next validation in that conversation, and every read reports them as `SUPERSEDED`. Eligibility never depends on that flag alone: it re-checks the revision, hashes, owner and rebuilt bank labels inside the caller's transaction. A changed bank label (e.g. card outstanding, payee suffix) makes a MATCH ineligible and allows one more bounded attempt. Derived amount evidence is frozen with the draft, included in `input_hash`, and never rebuilt from changed balances: a changed relevant balance requires a new draft. Pre-upgrade derived drafts without frozen inputs must also be redrafted. Remaining-balance lineage includes preceding debit parents; equity legs contribute actual share debit, while fraction dependencies retain the requested amount/budget semantics. A MATCH is never re-rolled while still bound; MISMATCH/UNCERTAIN are final for that draft version. A MATCH only permits confirmation; signature, ownership and hard policy still decide.

**Telemetry (T-08, `telemetry.py`):** workflow spans `ingest` (each user turn: a fresh trace in the conversation's session; turn text is never exported), `gateway.draft` → `agent.call` (its `traceparent` is sent to the agent, which continues the trace; the agent's freeze call back to `/internal/drafts` is the gateway's `compile` span in the same trace) → `semantic_validation` (the validator continues it again); `challenge` (envelope issuance; `linked_trace_id` = the validation trace, then an `awaiting_approval` event) with a nested `policy` span; `approval` (assertion outcome; nested `policy`, `step_up`, `execution`, `policy` and `receipt`); `step_up` (code submission; never the code); signed mandate changes as `challenge` / `approval` with `operation_type=update_mandate`. Expected outcomes are statuses (`clarification`, `blocked`, `expired`, `superseded`, `pending`, `rejected`, `timeout`), not errors. Spans are context managers inside one request, so none is open while a person decides. Audit rows written inside a span carry its trace/span IDs. Only allowlisted IDs and codes are attributes; user IDs are pseudonyms. Telemetry failures never change an outcome; `/health` shows `degraded`. Tests force a disabled facade for every test (`tests/conftest.py`) and use `Telemetry.for_testing` when they inspect spans.

**Known limits:** after a failed commit (e.g. the audit store was down) the authorization keeps its approval evidence and stays `PENDING_APPROVAL`; a new approve returns `ALREADY_APPROVED`, so the user requests a fresh envelope for the same draft. The approval window (≤120 s) also bounds `AWAITING_STEP_UP`: the step-up challenge expires with the signed envelope, because a commit after the signed expiry is refused anyway, so the user has the rest of that window to read the code; otherwise they request a fresh envelope. If a commit rolls back after a valid code, the code is invalidated in a follow-up transaction (fail closed) and the user requests a fresh envelope. The step-up channel and operator console are simulations (no SMS/push, not production MFA). A per-process `STEP_UP_SECRET` fallback means pending codes do not survive a restart. Pausing the mandate stops payments through this assistant only; it does not lock cards, accounts or other bank channels. `MandateChange` has no expiry field, so an expired mandate can be paused/decreased/relaxed but not renewed. Scam rules are keyword heuristics, not a fraud engine.

**Freeze error codes are stable** (`SOURCE_ACCOUNT_NOT_OWNED`, `PAYEE_NOT_SAVED`, `LEG_INVALID`, `AMOUNT_EXPR_INVALID`, `REFERENCE_NOT_FROM_USER`, …). A leg reference is signed and shown on the confirmation, so it must be the user's own words or the bill reference they saved for that biller.

**Drafting readiness.** `readiness.py` checks local token configuration, authenticated validator `GET /internal/ready`, then agent `GET /ready` before extraction: 1 s HTTP I/O limits and a 3 s total wall-clock cap. These are cheap service-local requests, not provider calls. `GET /ready` and `/api/ready` return 200/503 separately from liveness. Only stable codes leave failed checks; service bodies, headers and exception text are never forwarded or logged. Startup/health list missing configuration names and simulated step-up availability separately. See the root README for matching-token setup and restart requirements.

**Phase 4 drafting budget and reuse.** `DRAFT_TIMEOUT_SECONDS` (90 s, >0 and
≤180) covers threadpool admission, readiness, extraction, callbacks, validation and
response preparation. Agent and validator limits remain stage caps: each gets the
smaller of its cap and the remaining budget. `X-DCTA-Deadline` carries a UTC Unix
expiry between services, converted to local monotonic time on receipt; clocks on the
single host must agree. The browser cannot extend the gateway budget. Remote model
work can continue after transport cancellation; its result is discarded. Provider
queues check expiry again on admission. SDK extractor retries are disabled for
delegated runs so backoff cannot receive a fresh timeout.

The async route observes disconnects and signals the synchronous coordinator. Agent
callbacks use a signed run ID: closed, cancelled, expired or superseded runs are
refused, with another check inside the freeze/question write transaction. Audit-only
callbacks remain permitted under the token's ordinary TTL; mandatory gateway audit
and validation finalization are retained. A superseded request ends with
`CONVERSATION_CHANGED`; a new request is required for the new revision. Timing tests
allow 200 ms for thread scheduling, ASGI and local audit. Local mandatory audit may
need to finish after the response if SQLite is busy; this never grants eligibility.

Concurrent requests share work only for the same database, owner, conversation and
revision. **This registry is in memory and supports one gateway process only**, as
used by this app. Multiple gateway workers/replicas would need shared admission and
revocation state. Restart drops the registry and its delegation tokens; interrupted
validation retains existing recovery rules.

Validation-only retry compares canonical payload, conversation/hash/revision, bank
metadata, frozen calculation inputs, active mandate version and current unexpired
quote terms against stored evidence. Missing prior binding data requires extraction.
Edits or changed bindings rebuild the draft. Permanent configuration/request errors
and semantic MISMATCH/UNCERTAIN are not retried; unchanged final outcomes are surfaced.
The three-attempt, two-second retry and per-user rate limits still apply to reused
work. Settlement and receipt idempotency are unchanged. Restart gateway/agent/validator
after configuration changes; Compose forwards the overall budget to gateway only.

**Phase 5 progress and recovery:** `progress.py` holds process-local stage records for
at most 256 operation aliases, retaining completed records for 10 minutes. No schema
upgrade, setting or additional dependency is needed. `POST .../draft` accepts optional
`operation_id` (UUID), `revision` and `mode: "auto" | "validation"`; a mismatched
revision fails before extraction. Validation-only mode cannot start extraction when
unchanged frozen work is absent (`REDRAFT_REQUIRED`). Same-revision callers share the
existing work and progress, including cancellation.

`GET /api/conversations/{id}/progress?operation_id=...&revision=...` requires the
owner's session and returns stage, elapsed seconds, completion, stable failure code,
validation-retry availability and the stored outcome. Revision changes return
`CONVERSATION_CHANGED` without an old result; ready results recheck current semantic
eligibility and bank bindings before publication. Records are display/recovery data,
never approval evidence. They are lost on a gateway restart or eviction; authoritative
conversations, authorizations and receipts remain in SQLite. This matches the existing
single-process drafting admission; multiple gateway workers would require shared
operation storage or sticky routing.

Captured means the gateway has the input. The scoped agent reports `extracting` and
`resolving` on actual node entry through `POST /internal/conversations/{id}/progress`
(`conversation:read`, active run + owner + revision, only those two stages). The gateway
reports `validating`, `ready`, `needs_input` and `failed`. Agent reports cannot set a
validation verdict or confirmation eligibility. Voice uploads accept an optional UUID
`operation_id` query parameter; `transcribing` begins immediately before calling ASR,
and completed progress contains the gateway-produced turn at its new revision.

`POST .../draft/cancel` takes `operation_id` and `revision` and stops only that owned
drafting work. It never cancels an authorization, undoes a receipt or stops payment
execution. Superseded/cancelled work cannot publish late drafts or questions.
