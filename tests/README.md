# tests

Pytest suite (`make test` or `uv run pytest -q`). Each test uses its own temporary database, never `data/dcta.db`.

| File | Covers |
|---|---|
| `test_health.py` | Service health endpoints, no secrets in responses |
| `test_money.py`, `test_schemas.py`, `test_amount_expr.py` | Money rules, schema validation, relative-amount resolution |
| `test_canonical_golden.py` | Pinned canonical bytes, bundle hash and approval challenge |
| `test_lineage.py` | Critical-field lineage and taint through derived values |
| `test_boundary.py`, `test_isolation.py` | Import boundaries; read-only clients |
| `test_ledger.py`, `test_bank_read_api.py`, `test_delegation.py` | Mock bank, scoped reads, cross-user denial, delegation tokens |
| `test_conversations_audit.py` | Conversation store, corrections, audit chain tampering/concurrency/atomicity, agent impersonation |
| `test_compose_isolation.py` | Compose hardening and per-service secret distribution |
| `test_t05_enrollment.py`, `test_t05_sessions.py` | Passkey enrollment codes, login, sessions, Origin checks |
| `test_t05_drafts.py` | Freezing proposals: bank-resolved display data, ownership, evidence/lineage, ambiguity, reference-text amounts, relative amounts, equity terms, supersession |
| `test_t05_approval.py` | Fail-closed issuance, exact envelope, bad assertions (origin, RP, no UV, wrong challenge), single use, concurrency, expiry, supersession, operation mismatch |
| `test_t06_policy.py`, `test_t06_execution.py`, `t06_support.py` | Every policy boundary (KYC tier, mandate per-leg/daily/max legs, sanctions, funds, card overpayment, velocity incl. rolling window and SG midnight), scam signals, step-up gating, atomic multi-leg effects and rollback, idempotent receipts, lost-response retry, concurrency races |
| `test_t07_stepup.py`, `test_t07_mandates.py`, `t07_support.py` | Step-up code binding, attempts, expiry, replay, concurrency, hard blocks beat a valid code, operator channel auth; signed mandate changes, cooling, immediate tighten/pause, supersession |
| `test_t12_voice.py` | Voice upload: WAV rules, consent, audio stored before ASR with matching digest, ASR failures keep audio without a turn, duplicates, cross-user denial, hotwords from the user's own records, no audio/secrets in audit or telemetry |
| `test_t13_attacks.py`, `t13_support.py` | Rogue Agent Mode attack matrix (46 rows): malicious extractor + forced-MATCH validator vs the real gateway, approval/step-up/limit/concurrency/audit attacks |
| `test_orchestration.py`, `test_read_routes.py`, `test_gateway_telemetry.py`, `test_e2e_conversation_to_receipt.py`, `test_operator_console.py`, `orch_support.py` | Agent → validator orchestration, UI read routes, gateway spans, one full conversation-to-receipt flow, simulated phone CLI. `conftest.py` forces disabled telemetry for every test |
| `test_evals_harness.py` | Eval case schema, scoring, percentiles, fake-model dry run |
| `test_telemetry.py` | Telemetry allowlist and redaction (secret canaries), traceparent, health, cost unavailable vs configured |
| `test_amounts.py` | Amount phrase parser |
| `test_agent_workflow.py`, `test_agent_units.py`, `test_agent_telemetry.py`, `agent_support.py` | Agent graph with a fake LLM and fake gateway: all actions, clarifications (two Johns, missing source, ambiguous "half of it"), corrections, injection in references, malformed model output; tracing |
| `test_agent_gateway_integration.py` | Agent against the real gateway routes and evidence checker |
| `test_t10_gateway_validation.py`, `test_t10_validation_telemetry.py`, `t10_support.py` | Gateway validation coordinator: MATCH bound to the exact bundle/conversation/bank labels, MISMATCH/UNCERTAIN/ERROR handling with bounded retries, no client-supplied verdicts, supersession, forced-MATCH attacks still rejected by the gateway, full happy path; validator traces |
| `test_t10_validator.py`, `test_t10_validator_client.py` | Validator service: strict output checks (every malformed/contradictory reply → ERROR), service token, size limits, prompt/data separation; gateway client re-checks `input_hash` |
| `validator_fakes.py` | Fake LLMs for validator tests, incl. `AlwaysMatchLLM` for forced-MATCH attack tests |
| `t05_support.py`, `conftest.py`, `webauthn_soft.py` | Shared T-05 fixtures and a software passkey authenticator (ES256) that produces real attestation and assertion objects |
| `webauthn_soft.py` | Software authenticator for tests: ES256 key generation, registration/authentication responses verifiable by the `webauthn` library |
| `test_t05_enrollment.py` | Enrollment codes (create, consume, single-use, expiry), registration options, verification, wrong origin/RP/code |
| `test_t05_sessions.py` | Login, session cookie, authenticated routes, 401 on missing/invalid session, logout, rate limiting |
| `test_t05_drafts.py` | Freeze drafts, ownership validation, amount resolution, equity terms, invalid targets, bundle hash, authorization creation, TTL, user-scoped GET |
| `test_t05_approval.py` | Valid approval (signature verified but blocked), wrong owner/origin/RP/challenge, tampered payload, double approval, fail-closed hooks |

Container-level isolation is checked separately by `make isolation-probe` (Docker required).
