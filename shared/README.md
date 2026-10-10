# shared

Pure, side-effect-free building blocks used by every service. **No I/O, no database, and no imports of `gateway`, `ledger`, `agent` or `validator`** (enforced by `tests/test_boundary.py`).

| File | Purpose |
|---|---|
| `money.py` | `Money` over `Decimal`: SGD, max 2 dp, rejects floats/bool/NaN/negative, serialises as `"50.00"` |
| `amounts.py` | Deterministic amount-phrase parser ("fifty bucks", "a grand", "$1,250.50", "half of that", "the rest", "card balance"); used by the agent to normalize and by the gateway to check that cited words mean the proposed amount. Never guesses |
| `timeutil.py` | The single UTC timestamp format `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `schemas.py` | Strict Pydantic models: actions, legs, equity terms, relative amounts, `FrozenBundle`, `MandateChange`, `SigningEnvelope` (checks user/conversation binding and the 120 s TTL) |
| `lineage.py` | Where each critical value came from; free text (`reference_text`) can never supply an amount, recipient or source account, even when relabelled as derived |
| `canonical.py` | Canonical JSON, bundle hash, and the one approval-challenge function: `SHA256(b"DCTA:approval:v1\x00" + canonical_bytes(envelope))` |
| `settings.py` | Per-service settings; each service loads only the credentials it needs. `VALIDATOR_SERVICE_TOKEN` goes to gateway and validator only |
| `clients/` | Read-only HTTP clients for services that call the gateway, and the gateway's validator client/contract (`clients/validator.py`) |
| `telemetry/` | Safe Langfuse telemetry (T-08): workflow spans, LLM generations, allowlist/redaction before export, W3C `traceparent` propagation, pseudonymous user IDs, export health. Exports over the network only when constructed with Langfuse keys; importing it has no side effects. See `telemetry/README.md` |

The golden byte fixtures in `tests/test_canonical_golden.py` pin the signing format; any change to canonical bytes fails the tests.
