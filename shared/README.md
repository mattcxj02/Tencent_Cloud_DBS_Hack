# shared

Pure, side-effect-free building blocks used by every service. **No I/O, no database, and no imports of `gateway`, `ledger`, `agent` or `validator`** (enforced by `tests/test_boundary.py`).

| File | Purpose |
|---|---|
| `money.py` | `Money` over `Decimal`: SGD, max 2 dp, rejects floats/bool/NaN/negative, serialises as `"50.00"` |
| `amounts.py` | Deterministic amount-phrase parser ("fifty bucks", "a grand", "$1,250.50", "half of that", "the rest", "card balance"); used by the agent to normalize and by the gateway to check that cited words mean the proposed amount. Never guesses |
| `timeutil.py` | The single UTC timestamp format `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `schemas.py` | Strict Pydantic models: actions, legs, equity terms, relative amounts, `FrozenBundle`, `MandateChange`, `SigningEnvelope` (checks user/conversation binding and the 120 s TTL) |
| `clarification.py` | Pure strict contracts for grouped record candidates and selections bound to field IDs and payment legs; only the gateway can authenticate answers |
| `lineage.py` | Where each critical value came from; free text (`reference_text`) can never supply an amount, recipient or source account, even when relabelled as derived |
| `canonical.py` | Canonical JSON, bundle hash, and the one approval-challenge function: `SHA256(b"DCTA:approval:v1\x00" + canonical_bytes(envelope))` |
| `deadline.py` | Monotonic request budgets, bounded executor admission/calls, cancellation signals and `X-DCTA-Deadline` wire expiry; no service authority or ledger access |
| `settings.py` | Per-service settings; each service loads only the credentials it needs. `VALIDATOR_SERVICE_TOKEN` goes to gateway and validator only |
| `clients/` | Read-only HTTP clients for services that call the gateway, and the gateway's validator client/contract (`clients/validator.py`) |
| `telemetry/` | Safe Langfuse telemetry (T-08): workflow spans, LLM generations, allowlist/redaction before export, W3C `traceparent` propagation, pseudonymous user IDs, export health. Exports over the network only when constructed with Langfuse keys; importing it has no side effects. See `telemetry/README.md` |

The golden byte fixtures in `tests/test_canonical_golden.py` pin the signing format; any change to canonical bytes fails the tests.

Extractor timeout/retry settings are validated in `settings.py`:
`EXTRACT_TIMEOUT_SECONDS` (40 by default, >0 and ≤120) and `EXTRACT_MAX_RETRIES`
(0 by default, 0–2). Gateway `DRAFT_TIMEOUT_SECONDS` (90 by default, >0 and ≤180)
covers the entire draft request; per-stage limits are capped to its remaining budget.
Delegated extraction disables SDK retries; configured SDK retries remain available
for standalone extraction. Provider keys containing only whitespace count as missing.
Settings changes require service restarts.

The validator contract also carries `CalculationEvidence` and `CalculationDebit` with exact `Money` values. Calculation inputs are gateway-owned frozen data, included in the canonical request hash; they are not model reasoning or authorization.
