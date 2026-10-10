# shared/telemetry

Safe Langfuse telemetry for the gateway, agent and validator (T-08). Langfuse Cloud is the required backend; the in-memory exporter is for tests only.

**Trust boundary.** Everything here runs inside the calling service and may send sanitized telemetry to Langfuse. It never authorizes anything: a trace ID is correlation only and never selects a user, session or conversation. Nothing here raises into callers or changes a business outcome. No imports of `gateway`, `ledger`, `agent` or `validator`, and no `os`/`sys`/`logging`/`asyncio` (checked by `tests/test_boundary.py` and `tests/test_telemetry.py`).

| File | Purpose |
|---|---|
| `client.py` | `Telemetry` facade: `workflow_span`, `generation`, `event`, `health`, `check_backend`, `trace_url`, `read_back`, `flush`, `reexport_pending`, `shutdown`, `for_testing` |
| `redaction.py` | `redact_text`, `redact`, the attribute allowlist (`ALLOWED_ATTRIBUTES`) and value kinds, workflow stages and status levels |
| `propagation.py` | W3C `traceparent` parse/extract/inject, httpx request hooks, remote parent context, `pseudonymize` |
| `export.py` | `HealthState`, the `mask_otel_spans` hook (export-time allowlist) and `GuardedExporter` (scrub, count, bounded re-export buffer) |

## Use

One `Telemetry` per process, created from that service's settings; call `shutdown()` in the FastAPI lifespan.

```python
telemetry = Telemetry(load_agent_settings(), "agent")
with telemetry.workflow_span(
    "extract",
    session_id=conv_id,
    user_pseudonym=telemetry.pseudonymize(user_id),
    parent=extract_trace_context(request.headers),
) as span:
    with telemetry.generation(
        "extractor-llm-call", model=m, prompt_version=v, prompt_hash=h, input=messages
    ) as gen:
        gen.update(output=parsed, usage=resp.usage, latency_ms=ms)
    span.set_status("clarification", reason_code="AMBIGUOUS_PAYEE")
audit_row.trace_id = span.trace_id  # persist in audit
```

Outgoing internal calls: `httpx.Client(event_hooks={"request": [httpx_request_hook]})` (or `async_httpx_request_hook`). Incoming internal routes: `extract_trace_context(headers)` returns a validated `TraceParent` or `None`. Browser-facing gateway routes should not pass `parent` and start a fresh trace.

## Export pipeline and redaction

1. **Pre-sanitization** (before the SDK sees anything): payloads go through `redact` and a size cap (`max_field_chars`, default 8000 JSON chars); attributes go through `sanitize_attributes`.
2. **Langfuse `mask` hook**: a second `redact` over input/output/metadata.
3. **`should_export_span = is_langfuse_span`**: only spans from these helpers; no auto-instrumentation, no argument capture.
4. **`mask_otel_spans`**: deletes any span attribute that is not a known Langfuse field or an allowlisted metadata key, and re-runs `redact_text` on every exported string. Errors drop the batch (fail closed) and are counted.
5. **`GuardedExporter`**: drops all span events (exception messages/stack traces), redacts the status description, validates span names, counts results.

Key-based rules (`redact`): values under keys naming secrets (`authorization`, `cookie`, `*token`, `*secret`, `password`, `api_key`, `otp`, `code`, `pin`, `signature`, `assertion`, `clientDataJSON`, `authenticatorData`, `userHandle`, `credential*`, `nonce`, `private_key`, `headers`, …) become `[REDACTED]`. Audio/binary keys (`audio*`, `*base64`, `wav`, `pcm`, `bytes`, …) become `[REDACTED:binary]`. Account keys (`account_number`, `card_number`, `iban`, `nric`, `phone`, `proxy`, `email`, …) keep only an already-masked value or become `[REDACTED ending 1234]`. Bytes are never exported. Unknown objects become `<TypeName>` (never `repr`).

Text rules (`redact_text`): PEM private keys, `data:…;base64,` URIs, `Authorization`/`Cookie` header values, `Bearer …`, `key=value` secrets, `dcta_…` tokens, `sk-…`/`pk-lf-`/`sk-lf-`/AWS/GitHub/Slack keys and JWTs, OTP phrases (`otp 123456`, `code is 123456`), NRIC, e-mail, 8–19 digit runs (account/card/phone → `[REDACTED number ending 4521]`; ISO dates, UUIDs and decimals are kept), and ≥40-character base64 blobs that mix upper/lower case and digits (hex digests are kept). Masked suffixes like `ending 4521` pass through.

Allowlisted metadata (`ALLOWED_ATTRIBUTES`, each with a value kind): correlation IDs (`conversation_id`, `conversation_revision`, `bundle_id`, `bundle_version`, `bundle_hash`, `envelope_hash`, `authorization_id`, `audit_event_id`, `audit_seq`, `request_id`, `turn_id`, `turn_index`, `mandate_version`, `resumed_from_trace_id`, `linked_trace_id`), workflow facts (`outcome`, `reason_code`, `operation_type`, `action_count`, `leg_count`, `policy_decision`, `validator_verdict`, `semantic_check`, `clarification_field`, `approval_state`, `step_up_required`, `risk_band`, `channel`, `attempt`, `http_status`, `route`, `error_type`, `synthetic`), display values (`amount`, `currency`, `recipient_alias`, `account_masked`) and model facts (`model`, `provider`, `prompt_version`, `prompt_hash`, `latency_ms`). Integers above 7 digits are dropped as account-like. Reserved keys (`service`, `stage`, `usage_status`, `cost_*`, …) are set only by the telemetry layer. Dropped keys are counted in `dropped_attribute_count`.

## Status, usage and cost

`set_status` values: `ok`, `pending`, `clarification`, `superseded` → DEFAULT; `blocked`, `rejected`, `timeout`, `expired`, `cancelled` → WARNING; `error` → ERROR. Exceptions are recorded as `error: TypeName` without their message; exceptions with `telemetry_status`/`reason_code`, 4xx `status_code`, or timeouts map to expected outcomes. Reason codes must be UPPER_SNAKE (`HARD_LIMIT`, `HTTP_403`); codes containing digits need an underscore, so a one-time code on an exception's `code` attribute is never exported.

Usage is recorded when supplied (OpenAI or Langfuse keys, including reasoning tokens). Cost is recorded only from an explicitly configured `ModelPrice` (USD per million tokens); otherwise `cost_status=unavailable` with a reason and no cost field, never zero.

## Health and outages

`health()` → `{"backend": "langfuse", "state": "ok" | "degraded" | "disabled", "last_error", "dropped", "pending", "exported", "service"}`. Missing keys → `disabled` and no-op spans. Export failure → `degraded`; failed batches (already sanitized) wait in a bounded buffer (`max_pending_spans`, oldest dropped and counted) and are retried on the next successful export, on `flush()` and on `reexport_pending()`. Only telemetry is re-sent; no business operation is replayed. The SDK's own batch queue (OpenTelemetry default 2048 spans) drops on overflow with a log line that is not counted here.

## Limitations

- Pseudonyms use HMAC with a public prototype context string (plus an optional `pseudonym_key`); a guessable user ID can be confirmed by recomputing. Production would use a KMS-held key shared by all services.
- Redaction is pattern-based. It is a backstop; callers must still pass only synthetic, model-visible data and use allowlisted attributes.
- Langfuse keeps one pipeline per public key per process, so several `Telemetry` objects with the same key share one exporter and health state. `shutdown()` removes the SDK singleton (private API) so a later instance gets a fresh pipeline.
- `read_back` depends on ingestion lag (typically 15–60 s) and the v2 observations API; the legacy `/api/public/traces/{id}` returns 410 for this organization.
