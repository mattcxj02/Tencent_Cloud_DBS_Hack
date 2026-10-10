# T-08 telemetry evidence

Date: 10 Oct 2026. Shared helpers in `shared/telemetry/` (built by a Claude subagent, reviewed by the lead). Not yet wired into the three services.

## Live Langfuse round trip (`make smoke-telemetry`)

Re-run by the lead: all 9 checks passed. Six observations in one trace: `ingest` → `extract` → generation `extractor-llm-call`; `policy` (blocked, WARNING); `semantic_validation` continued from a second service instance via `traceparent`; `execution` (ERROR, exception type only). The user ID is pseudonymous, a session ID is set, and planted fake secrets (API key, Bearer token, `dcta_` token, OTP, full account number) are absent from the stored data. Read back through `GET /api/public/v2/observations` (legacy trace API returns 410 for this organization).

Subagent run: trace `d8f8c88ddb5ec26607751079ed2b9d13`, https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/traces/d8f8c88ddb5ec26607751079ed2b9d13 (usage recorded; cost recorded as unavailable, not zero).

## Independent canary probe (lead)

In-memory exporter, separate canaries (`sk-live-…`, `dcta_…`, `Bearer …`, a 12-digit account number, an OTP phrase, a WebAuthn signature): none exported; allowlisted `bundle_id` and `reason_code` kept.

## Tests

`tests/test_telemetry.py`: 61 passed.
