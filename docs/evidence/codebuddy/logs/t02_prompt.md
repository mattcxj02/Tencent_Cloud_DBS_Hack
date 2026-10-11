You are implementing task T-02 of this repository. First read CODEBUDDY.md, then the T-02 section of docs/instructions.md, and in docs/PLAN.md the sections "Workflow and service ownership", "Mandates, risk and transaction semantics" and "Stack and layout". Follow them exactly. Do only T-02; do not start T-03.

Hard rules (non-negotiable):
- NEVER read, print, cat, grep, edit or create `.env`. Do not commit, push or change git config. Do not modify `references/`, PLAN.md, instructions.md, ARCHITECTURE.md or CODEBUDDY.md.
- `shared/` stays pure (no gateway/ledger imports, no DB). `agent/` and `validator/` must never import `gateway` or `ledger`.
- No new runtime dependencies unless essential; explain any you add. Use stdlib `sqlite3`. Run everything with `uv run`.

Existing context (T-00, T-01 done): `shared/money.py` (Money over Decimal, 2-dp strings, rejects floats), `shared/schemas.py` (Action, TargetType, Leg, EquityTerms, FrozenBundle, MandateChange, SigningEnvelope), `shared/canonical.py`, `shared/lineage.py`, `shared/timeutil.py` (UTC ms `Z` strings), `shared/settings.py` (GatewaySettings has `db_path` default `data/dcta.db`). Gateway FastAPI app at `gateway/app.py` (port 8002). Tests: `uv run pytest -q`; lint `uv run ruff check .`; format touched files with `uv run ruff format`. Reuse these types; do not duplicate them.

Build:

1. **Ledger schema and access (`ledger/`)**, used only by gateway code. SQLite with `PRAGMA foreign_keys=ON`, WAL, and money as INTEGER minor units (cents) — never REAL. Convert at the boundary to/from `Money`. Tables (adjust names sensibly): users; kyc (status verified/unverified/expired, tier 1/2/3, expires_at); accounts (id, type savings/current/joint, masked suffix, balance_cents); account_holders (user_id, account_id, role owner/joint) so a joint account can be operable by two users; payees (owner user_id, display name, bank, masked suffix, kind bank/paynow, sanctioned flag) and payee_aliases (alias text → payee); billers (+ per-user saved biller links with bill reference); cards (owner, masked suffix, outstanding_cents); holdings; equity_quotes (quote_id, symbol, unit_price_cents, quoted_at, expires_at); mandates (user, version, status active/pending/paused, per-leg and daily limits, allowed actions, effective_at); transactions and receipts (tables only; writes come in T-06). Use a schema version table and an idempotent `init_db()`.
2. **Seed (`ledger/seed.py`, wire `make seed`)**, idempotent (running twice yields identical state). Synthetic data only:
   - Matthew (`user-matthew`): KYC verified tier 2, expires 2027-12-31. Accounts: savings 12000.00, current 3500.00, joint 2000.00 (joint also held by user 2).
   - Payees: Mum (alias "Mum"), Ah Ma (alias "Ah Ma"), John Doe ending 4521, John Tan ending 8892, Landlord Lim, Northwind Trading (sanctioned=true, synthetic), one saved PayNow recipient (e.g. "Priya", masked mobile).
   - Biller SP Group (saved, with a bill reference); owned credit card outstanding 642.30.
   - Mock SGX quotes basis prices for D05, O39, U11, Z74 (two-decimal SGD); holdings optional.
   - Active mandate v1: per-leg 1000.00, daily 3000.00, all four actions allowed.
   - Second user (`user-ana` or similar): own savings account, own payee list, KYC verified tier 1, holder on the joint account.
3. **Quotes.** A gateway function `get_quote(symbol, now)` creates a new quote row with `expires_at = now + 5 minutes` (fixed mock price per symbol; clock injectable). Quotes are never mutated; expired quotes stay expired.
4. **Service-to-gateway identity (interim, until T-05 adds user sessions).** Implement `gateway/auth/delegation.py`: the gateway issues short-lived HMAC-SHA256 tokens binding `{user_id, conversation_id (optional), scopes, issued_at, expires_at, token_id}` with a gateway-only secret (`GATEWAY_DELEGATION_SECRET` from settings; if unset, generate a random per-process secret and log a warning — never a hard-coded default). Verify with `hmac.compare_digest`, reject expired/tampered/wrong-scope tokens. A FastAPI dependency `require_principal(scope)` returns a `Principal(user_id, scopes, conversation_id)`. The caller can never choose the user by header or query param; the user comes only from the verified token. Tests mint tokens with the real issuer; do NOT add any runtime bypass flag or header. (T-03 will hand these tokens to the agent; T-05 adds browser sessions.)
5. **Scoped read API** on the gateway under `/internal/bank/...` requiring scope `bank:read`, always filtered to `principal.user_id`: accounts with balances (only accounts the user holds); payee search returning ALL matching candidates for a name/alias (case-insensitive exact alias match plus case-insensitive token/substring match on display name — e.g. "john" returns both Johns; never auto-select, never rank-and-pick); billers and saved bill references; cards with outstanding balance; holdings; quote issue/fetch; recent transaction history. Responses use Pydantic models with Money as 2-dp strings and display metadata from bank records only. Another user's resource IDs return 404 (not 403) to avoid existence leaks. Bound list sizes and query length.
6. **READ/DRAFT client.** `shared/clients/bank_read.py`: a small httpx client with read methods only, taking the delegation token; no execute/write methods exist anywhere outside gateway. Add `shared/clients/__init__.py`.
7. **Do not** infer amounts from history, fuzzy-pick a payee, or let caller-supplied display names override bank records.

Tests (meaningful):
- `init_db` + seed twice → identical row counts and balances (idempotent).
- Money round-trips exactly through INTEGER cents (e.g. 642.30, 0.01, 999999.99); REAL never used (assert column types via `PRAGMA table_info`).
- Cross-user: user-ana cannot read Matthew's accounts/payees/cards (404); both can see the joint account.
- Payee search "john" returns John Doe and John Tan; "Ah Ma" exact alias; "Northwind" returns it flagged sanctioned; empty/oversized query rejected.
- Delegation token: valid; expired; tampered payload; wrong scope; wrong signature/secret; user cannot be overridden by any header/query param.
- Quote: expires 5 minutes after the injected `now`; expired quote fetch reports expired.
- Boundary: AST test that agent/ and validator/ do not import gateway or ledger, and that `shared/clients` has no write/execute methods (e.g. no POST other than quote issue — document the exception).
- Use a temp DB path per test (fixture), never `data/dcta.db`.

Finish: `uv run ruff format` on touched files, `uv run ruff check .`, `uv run pytest -q`; all must pass. Run `make seed` once against a temp path (`DCTA_DB_PATH=/tmp/... make seed` or equivalent) to prove it works. Update only the T-02 row in docs/STATUS.md (`[x]` only if all pass). Final message: files changed, schema summary, the delegation-token format, checks and results, limitations.
