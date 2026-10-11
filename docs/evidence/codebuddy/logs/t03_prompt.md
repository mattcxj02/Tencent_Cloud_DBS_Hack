You are implementing task T-03 of this repository. First read CODEBUDDY.md, the T-03 section of docs/instructions.md, and in docs/PLAN.md "Workflow and service ownership" plus "Langfuse backend and audit — both required" (audit paragraphs). Follow them exactly. Do only T-03; do not start T-04/T-05.

Hard rules (non-negotiable):
- NEVER read, print, cat, grep, edit or create `.env`. Do not commit, push or change git config. Do not modify `references/`, PLAN.md, instructions.md, ARCHITECTURE.md or CODEBUDDY.md.
- `shared/` stays pure; `agent/` and `validator/` never import `gateway` or `ledger`. No new runtime dependencies unless essential (explain). Run everything with `uv run`.
- No runtime bypass flags or headers. Tests use FastAPI `app.dependency_overrides` or the real token issuer only.

Existing context (T-00–T-02 done, 202 tests): `ledger/schema.py` (SQLite, `init_db()`, `connect()`, INTEGER cents, WAL, FKs), `ledger/seed.py`, `gateway/bank.py` (BankStore), `gateway/bank_routes.py` (`/internal/bank/*`), `gateway/auth/delegation.py` (HMAC delegation tokens → `Principal(user_id, scopes, conversation_id, token_id)`, `require_principal(scope)`), `shared/canonical.py` (`canonical_bytes`, sha256 helpers), `shared/timeutil.py` (UTC ms Z), `shared/schemas.py`. Reuse these; do not duplicate canonicalization or time formatting. Read them before designing.

Build:

1. **User-session dependency placeholder.** `gateway/auth/session.py` with `require_user_session()` returning a `UserPrincipal(user_id)`. Until T-05 implements WebAuthn sessions it must FAIL CLOSED (401 "user sessions not implemented until T-05") for every request. Tests override it via `app.dependency_overrides` only. Document this clearly.

2. **Gateway-owned conversation store** (tables in the same gateway-owned SQLite DB via `ledger/schema.py` migrations, schema version bump; code in `gateway/conversations.py` + routes). Server assigns all ids, roles, ordering, revisions and hashes:
   - Turn roles: `user_text`, `user_asr_final`, `user_transcript_edit` (edits a prior turn: keeps the original, records the new text with `edits_turn_id`), `app_question` (a clarification question the gateway records; text may originate from an agent proposal but is labelled as application output), `user_answer` (tied to `answers_question_id`). Bank data is NOT a turn.
   - Every user-originated append increments `conversation.revision`. `conversation_hash` = sha256 of canonical bytes of the ordered turn list (turn_id, seq, role, text, edits_turn_id, answers_question_id, created_at) plus conversation_id and revision. Retain all originals; nothing is deleted or rewritten.
   - Bounds: max text per turn (e.g. 2,000 chars), max turns and total characters per conversation (e.g. 40 turns / 20,000 chars). Exceeding returns a stable error code `CONTEXT_LIMIT_RESTATE` asking for an explicit restatement; never truncate silently.
   - Audio evidence: `POST` upload stores raw bytes under a gateway-only directory (configurable, default `data/audio/`), recording sha256 digest, content type, size, duration if WAV header parses, consent flag and retention-until, linked to the conversation and optionally a turn. Accept only `audio/wav` up to 3 MB for now (TRTC sentence limit). A transcript is never treated as a saved recording. ASR is T-12; do not call TRTC here.
3. **Routes.**
   - User (via `require_user_session`, owner-checked; other users' conversations → 404): create conversation; append text turn; append transcript edit; append answer to a question; upload audio; get own conversation; get own audit events and a chain verification result.
   - Agent/internal (delegation token): `GET /internal/conversations/{id}/snapshot` requires scope `conversation:read` AND `principal.conversation_id == id` AND principal.user_id owns it; returns ordered turns, revision, hash. `POST /internal/conversations/{id}/questions` requires `conversation:propose_question` and records an `app_question` (does not bump the user revision). There is NO internal route to append user turns; agent credentials cannot create `user_*` roles by any path.
   - `POST /internal/audit/agent-events` requires `audit:append_agent`; the event `step` must start with `agent.` from an allowlist (e.g. agent.extract, agent.normalize, agent.resolve, agent.clarify, agent.propose); it is stored with `source="agent"` and cannot use gateway-trusted step names (e.g. anything under `gateway.`, `approval.`, `execution.`).
4. **Prototype hash-chained audit** (`gateway/audit.py`, table `audit_events`): columns seq (INTEGER PK, gapless), ts, source (gateway|agent|validator), session_id/conversation_id, user_id, step, payload_json (canonical), trace_id, span_id, prev_hash, row_hash. Genesis prev_hash = 64 zeros. row_hash = sha256(canonical bytes of the row contents excluding row_hash, including prev_hash). Provide `append_audit(conn, ...)` that MUST be called inside the caller's open write transaction (BEGIN IMMEDIATE) so gateway financial events and audit commit atomically; reading the tail and inserting happen under that same lock. Provide `verify_chain(conn, conversation_id=None)` returning ok + first bad seq. Payloads must not contain secrets or raw audio (store digests/ids).
5. **Supersession.** `supersede_for_conversation(conn, conversation_id, new_revision)` marks pending records bound to older revisions as SUPERSEDED. Later tasks register their tables (drafts, validations, authorizations, step-ups) in a module-level registry `REVISION_BOUND_TABLES`; T-03 implements the mechanism and tests it with a test-only table created in the test. Call it on every user-originated append, inside the same transaction, and audit it.
6. Gateway trace/span id fields accept optional W3C-style ids from the request context later (T-08); for now nullable.

Tests (meaningful; temp DB per test; never `data/dcta.db`):
- Tampering with any audit row (payload, step, prev_hash) is detected at the right seq; deleting a middle row is detected; note in a test docstring that tail truncation and a privileged full rewrite are NOT detectable without external checkpoints.
- Concurrent appends (threads, each with its own connection) produce one valid gapless chain.
- Atomicity: inside one BEGIN IMMEDIATE, write a test ledger row + `append_audit`; force the audit append to raise → ledger row is rolled back. Also the reverse path commits both.
- Agent cannot impersonate: no route accepts a `user_*` role from a delegation token; agent-event route rejects `gateway.*`/`approval.*`/`execution.*` steps and non-allowlisted steps; snapshot with a token for another conversation or another user → 403/404.
- Cross-user: user B cannot read user A's conversation, audit or audio (404).
- Correction: a transcript edit keeps the original turn, bumps revision, changes conversation_hash, and supersedes a pending test record bound to the old revision.
- Context bound returns `CONTEXT_LIMIT_RESTATE`; oversized/non-WAV audio rejected; audio digest matches stored bytes.
- `require_user_session` without override returns 401 (fail closed).

Finish: `uv run ruff format` touched files, `uv run ruff check .`, `uv run pytest -q`; all must pass. Update only the T-03 row in docs/STATUS.md (`[x]` only if all pass). Final message: files changed, table/route summary, hash definitions, checks and results, limitations.
