# Build instructions: CodeBuddy task list

How this works:
- **Where** says how the task runs. `IDE` means Claude opens it in CodeBuddy with
  `buddy chat -m agent -r "<prompt>"`; you watch it and **take a screenshot**, which counts as proof of use.
  `CLI` means Claude runs it headless with `codebuddy -p`, and the JSON log is saved in `docs/devlog/`.
- After each task, Claude reviews `git diff`, runs the acceptance checks, then commits locally. **No pushes.**
- CodeBuddy reads `CODEBUDDY.md` automatically, so prompts don't repeat the rules.

Status legend: ⬜ todo · 🟨 in progress · ✅ done

---

## Phase P0: Setup (1 Oct, ~3 h)

### T-00 · Python + React scaffold · CLI · ⬜
**Prompt**
> Scaffold the repository described in CODEBUDDY.md.
> 1. Create a uv project (Python 3.12) with packages `agent`, `gateway`, `ledger`, `shared` (each with
>    `__init__.py`), plus `tests/` and `evals/`. Add deps: fastapi, uvicorn[standard], pydantic,
>    pydantic-settings, webauthn, langgraph, openai, langfuse, python-dotenv. Dev deps: pytest, ruff, httpx.
> 2. `shared/config.py`: a pydantic-settings `Settings` class reading `.env` (LLM_BASE_URL, LLM_API_KEY,
>    EXTRACT_MODEL, VALIDATE_MODEL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, RP_ID=localhost,
>    RP_ORIGIN=http://localhost:5173, DB_PATH=data/dcta.db, NONCE_TTL_SECONDS=120,
>    MANDATE_PER_TXN_MAX=1000.00, MANDATE_DAILY_MAX=3000.00). Write `.env.example` with placeholders.
> 3. `.gitignore` for Python, Node, `.env`, `data/`, `*.pdf`.
> 4. In `web/`: a Vite React TypeScript app with Tailwind configured, a single placeholder page titled
>    "DCTA", and a Vite dev proxy: `/api/agent` → http://localhost:8001, `/api/gateway` → http://localhost:8002.
> 5. A `Makefile` with targets: `test`, `lint`, `agent` (uvicorn agent app on 8001),
>    `gateway` (uvicorn gateway app on 8002), `web` (npm run dev). Stub minimal FastAPI apps with `/health`.
> 6. One test `tests/test_health.py` hitting both `/health` endpoints with FastAPI TestClient.

**Acceptance:** `uv run pytest -q` passes · `uv run ruff check .` clean · `cd web && npm run build` succeeds.

### T-00b · Accounts (you, manual) · ⬜
- Langfuse Cloud (free Hobby tier): create a project and copy its keys into `.env` (never into chat).
- TokenHub: claim the new-user free trial and tick **Hy3**, **DeepSeek-V4-Flash** and **Hy4 Preview**; key goes in `.env`.
- Put your personal DeepSeek key in `.env` as a dev alternative (`LLM_BASE_URL=https://api.deepseek.com`).

---

## Phase P1: Deterministic core (2–4 Oct, ~10 h, no LLM needed)

### T-01 · Schemas + canonical JSON · **IDE (screenshot)** · ⬜
**Prompt**
> Implement `shared/schemas.py` and `shared/canonical.py`.
> - Enums: `Action` = transfer | pay_bill | pay_card | buy_equity. `Provenance` = user_speech | bank_record |
>   untrusted_text | derived.
> - `Leg`: leg_id (str, "L1"…), action, source_account_id, target_id (payee/biller/card id or SGX symbol),
>   target_display (str, from bank records), amount (Decimal, >0, 2 dp), currency ("SGD" only),
>   quantity (optional int, buy_equity only), reference (optional str ≤ 35 chars),
>   provenance: dict[field_name, Provenance].
> - `DraftBundle`: bundle_id (uuid4 str), user_id, legs (1–4), created_at (UTC), transcript_sha256 (hex).
> - `Mandate`: mandate_id, user_id, per_txn_max, daily_max, allowed_actions, payees ("saved_only"),
>   max_legs_per_bundle, valid_until, effective_from (for cooling), status (active|paused).
> - `canonical.py`: `canonical_bytes(bundle) -> bytes` producing deterministic JSON: sorted keys,
>   no whitespace, UTF-8, Decimals as 2-dp strings, datetimes as ISO-8601 Z.
>   `signing_hash(canonical: bytes, nonce: str, expiry: datetime) -> bytes` = SHA-256 over
>   canonical ‖ b"|" ‖ nonce ‖ b"|" ‖ expiry-ISO.
> - Validation rule (I7): a Leg is invalid if provenance of `target_id` or `amount` is `untrusted_text`.
> - Tests: same bundle gives identical bytes across runs and key orders; changing any amount by 0.01 changes
>   the hash; float amounts are rejected; I7 rule rejects.

**Acceptance:** tests pass; `canonical_bytes` output contains no spaces and `"amount":"50.00"`.

### T-02 · Hash-chained audit log · CLI · ⬜
**Prompt**
> Implement `shared/audit.py`: an append-only SQLite table `audit_log(seq INTEGER PK, ts, session_id, step,
> payload_json, trace_id, prev_hash, row_hash)`. `row_hash = sha256(prev_hash ‖ canonical(seq, ts,
> session_id, step, payload_json, trace_id))`; the genesis prev_hash is 64 zeros. Functions: `append(...)`,
> `verify_chain() -> (ok: bool, first_bad_seq: int | None)`, `get_session(session_id)`. No UPDATE/DELETE
> functions. Tests: 100 appends verify ok; tampering with one row's payload via raw SQL makes
> verify_chain report that exact seq.

**Acceptance:** tests pass; the tamper test identifies the correct row.

### T-03 · Mock core bank + seed data · CLI · ⬜
**Prompt**
> Implement `ledger/`: SQLite tables for users (id, name, kyc_tier 1–3), accounts (id, user_id, type
> savings|current|joint, masked_no, balance Decimal), payees (id, user_id, display_name, aliases JSON,
> bank, masked_no, created_at, is_sanctioned bool), billers, cards (outstanding balance), holdings, equities
> (SGX symbol, name, mock price), transactions. `ledger/seed.py` creates one demo user "Matthew" (KYC tier 2) with:
> savings S$12,000, current S$3,500, joint S$2,000; payees: "Mum" (aliases "mum","mummy","mom","mother"),
> "Ah Ma" (aliases "grandma","ah ma","ama"), "John Doe" ••4521, "John Tan" ••8892, "Landlord Lim"; one sanctioned payee
> "Northwind Trading" for the demo; biller SP Group; one credit card with S$642.30 outstanding; equities
> D05 (DBS), O39 (OCBC), U11 (UOB), Z74 (Singtel) with mock prices; 20 historical transactions (John Doe
> usually receives ~S$50, John Tan ~S$5,000 → used later for magnitude checks). Read-only query helpers
> go in `ledger/queries.py`; write functions go in `ledger/writes.py` (only the gateway may import this).
> Tests: seed is idempotent; balances sum correctly.

**Acceptance:** `uv run python -m ledger.seed` creates `data/dcta.db`; tests pass.

### T-04 · Skill registry + import boundary test (I1) · **IDE (screenshot)** · ⬜
**Prompt**
> Implement `shared/skills.py`: a registry where each skill is a typed function registered with a
> side-effect class `READ | DRAFT | EXECUTE` via decorator `@skill(kind=...)`, with Pydantic input/output
> models and a name. Provide `list_skills(kind)`, `call(name, args)` which validates input/output and
> writes an audit entry. Register READ skills wrapping `ledger/queries.py` (lookup_payee with alias +
> fuzzy match returning *all* candidates, list_accounts, get_balance, get_quote, get_history).
> Write `tests/test_invariants.py`: (a) statically walk every `.py` under `agent/` with `ast` and fail if
> any import references `gateway` or `ledger.writes`; (b) assert the agent-side registry exposes no
> EXECUTE skills.

**Acceptance:** tests pass; adding `import ledger.writes` to any agent file makes the test fail (Claude checks this, then reverts).

### T-05 · Gateway: identity, challenge, verify · **IDE (screenshot)** · ⬜
**Prompt**
> Implement in `gateway/`:
> - `identity.py`: WebAuthn registration + login using `py_webauthn` (RP_ID/RP_ORIGIN from settings),
>   credentials stored in SQLite; `userVerification="required"`.
> - `POST /gateway/challenge` body = DraftBundle → store a pending record {nonce (random 32 bytes b64url),
>   expiry = now+NONCE_TTL, canonical bytes, hash}; return {nonce, expiry, canonical (b64), challenge
>   (b64url of signing_hash)}. The gateway computes canonical bytes itself (never trusts client bytes).
> - `POST /gateway/execute` body = {nonce, canonical_b64, assertion}. Checks in this order, each failure →
>   specific 4xx + audit entry: nonce exists & unused (I4) → not expired → sha256 recomputed from received
>   canonical equals stored hash → WebAuthn assertion verifies with expected_challenge = that hash and
>   require_user_verification=True (I2) → mark nonce used. Do not execute legs yet (T-06); return
>   {"verified": true}.
> - `tests/soft_authenticator.py`: a software ES256 authenticator (using `cryptography`) that can register
>   and produce valid assertions with the UV flag, so tests run without a browser.
> - Tests: happy path verifies; replayed nonce → 409; expired → 410; tampered canonical (amount +1) → 400
>   hash mismatch; assertion signed over a different challenge → 401; UV flag missing → 401.

**Acceptance:** all 6 tests pass.

### T-06 · Gateway: policy + execution · **IDE (screenshot)** · ⬜
**Prompt**
> Implement `gateway/policy.py` and `gateway/execute.py`, wired into `/gateway/execute` after verification.
> Policy (deterministic, returns PASS | STEP_UP(reasons) | BLOCK(reasons)): active mandate exists and
> not paused; action allowed; leg count ≤ max; each leg ≤ per_txn_max; today's executed total + bundle ≤
> daily_max; payee is saved (else STEP_UP); payee sanctioned → BLOCK; KYC tier limits (tier1 ≤ S$500/day,
> tier2 ≤ S$5,000, tier3 ≤ S$20,000) → BLOCK; velocity: >5 transfers in 10 min → BLOCK; sufficient balance.
> Mandate changes: `POST /gateway/mandate` requires a signed challenge like transactions; increases get
> effective_from = now + 12 h, decreases/pauses are immediate.
> STEP_UP: return 428 with reasons and a step-up id; `POST /gateway/stepup/{id}` accepts a mock TOTP
> (fixed secret from settings, verified with `pyotp`) and then executes.
> Execution: legs in order inside one SQLite transaction (all-or-nothing), idempotent on bundle_id.
> buy_equity: quantity = floor(amount / price). Every decision → audit.
> Tests for every rule, mandate cooling (use a frozen clock), and all-or-nothing rollback.

**Acceptance:** tests pass; `make gateway` starts; `/docs` shows the endpoints.

### T-07 · Attack test suite (Rogue Agent groundwork) · CLI · ⬜
**Prompt**
> Add `tests/test_attacks.py` covering, end to end through the gateway HTTP API: execute without any
> signature; signature by an unregistered credential; valid signature reused on a second bundle; bundle
> altered after signing (payee swap); amount split into 6 small transfers (velocity); transfer to
> sanctioned payee; mandate increase used before cooling ends; direct import attempt of `ledger.writes`
> from an agent module (should be caught by the invariant test). Each test asserts the exact rejection
> and that an audit entry was written.

**Acceptance:** all attack tests pass; `verify_chain()` still OK after the run.

---

## Later phases (task prompts written after P1 review)
- **P2 Agent graph (5–8 Oct):** firewall, number parser, extract (LLM), resolve, clarify, compile,
  validate (LLM), pre-check, Langfuse tracing.
- **P3 React UI (9–11 Oct):** chat + mic + text, overlay, WebAuthn, audit viewer, mandate screen.
- **P4 Risk/demo (12 Oct) · P5 Eval + deploy (13 Oct) · P6 Submission (14–15 Oct).**
