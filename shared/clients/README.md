# shared/clients

Scoped HTTP clients between services: non-gateway services calling the gateway, and the gateway calling the internal validator.

| File | Purpose |
|---|---|
| `bank_read.py` | Read-only bank client (accounts, payee search, billers, cards, holdings, quotes, history) authenticated with a delegation token |
| `validator.py` | Gateway -> validator contract (T-10): strict request/result schemas, stable error codes, `validation_input_hash` (SHA-256 of the canonical request), the request and model-output reference checks shared with the validator, and `ValidatorClient`, which sends `VALIDATOR_SERVICE_TOKEN` as a bearer token, enforces a timeout and turns every transport, HTTP or response failure into an `ERROR` result. It re-checks the returned `input_hash` and issue references; a MATCH is confirmation eligibility only |

Only READ (and, later, DRAFT/proposal) methods belong in the gateway clients; `validator.py` only requests a semantic assessment. Execute or ledger-write methods must never exist outside `gateway/`; `tests/test_isolation.py` checks this. Requesting a quote is the one POST, and it only creates an immutable mock quote.
