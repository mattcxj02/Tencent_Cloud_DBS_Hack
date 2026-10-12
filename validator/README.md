# validator

The independent semantic validator service (FastAPI, port 8003, reachable only from the gateway). It compares the user's full conversation with the frozen bundle and returns `MATCH`, `MISMATCH` or `UNCERTAIN`. A `MATCH` only allows the confirmation screen; it never authorizes execution. A schema-valid verdict can still be wrong.

| File | Purpose |
|---|---|
| `app.py` | `GET /health` (token/model/prompt-hash status, no secrets) and `POST /internal/validate`. Checks the service token (constant-time), body size (256 KiB), strict request schema, context budget, conversation consistency, bundle/conversation binding and bank-label coverage before any model call. Traces each authenticated validation (see Telemetry below) |
| `prompt.py` | Loads the fixed prompt `prompts/validator.md` once and exposes `PROMPT_HASH` (SHA-256 of the file). Builds exactly two messages: system = prompt only; user = request data as JSON between fixed markers, with `<`/`>` escaped so data cannot forge a marker. Bundle lineage is not shown to the model |
| `llm.py` | `LLMClient` interface and the OpenAI-compatible client: temperature 0, `response_format` JSON object, no tools, `max_retries=0`; `reasoning_content` is ignored |
| `service.py` | One model call under a wall-clock deadline (`VALIDATE_TIMEOUT_SECONDS`, default 30), then strict output checks. Every failure is `status: ERROR` with a stable code, never a MATCH |

The request/response schemas, error codes, input hash and output reference checks live in `shared/clients/validator.py` so the gateway can use them too.

**Contract.** `POST /internal/validate` with `Authorization: Bearer <VALIDATOR_SERVICE_TOKEN>` and a `ValidationRequest`: `request_id`, `conversation` (`conversation_id`, `revision`, `conversation_hash`, ordered `turns` with `turn_id`, `seq`, `role`, `text`, `edits_turn_id`, `answers_question_id`), `bundle` (the stored `FrozenBundle`) and `bank_metadata` (`accounts`, `targets` with display labels), plus `calculation_evidence` (frozen exact arithmetic inputs and result for derived amounts). Calculation consistency is checked before calling the model. Prompt v2 separates semantic fidelity from deterministic policy: ignored reference/pasted instructions do not by themselves imply semantic uncertainty; unsupported fields or extra legs still mismatch, and ambiguous intent still fails closed. Scam step-up and hard blocks remain gateway decisions. Unknown fields, including any extractor reasoning or prompt override, are rejected.

- 401 `UNAUTHORIZED`, 503 `SERVICE_TOKEN_UNSET` (no token of >= 32 chars configured: every request is refused), 413 `CONTEXT_LIMIT_RESTATE` (over 40 turns, 2,000 chars per turn or 20,000 total; nothing is truncated), 422 `INVALID_REQUEST` / `CONVERSATION_INVALID` / `BINDING_MISMATCH` / `BANK_METADATA_INCOMPLETE`. Body: `{"status": "ERROR", "code": ...}`. No model call.
- Otherwise 200 with `{status, verdict?, issues?, code?, model, prompt_hash, input_hash, latency_ms, usage?}`. `input_hash` is SHA-256 of the canonical request. Model output must be exactly `{verdict, issues}`; MATCH needs zero issues, MISMATCH at least one; leg IDs and evidence spans must exist in the request. Anything else, plus timeout, provider error, empty, truncated or tool-call replies, gives `status: ERROR`.

**Boundary:** no execution tools, ledger access or signing keys; imports `shared/` only (never `gateway`, `ledger`, `agent` or `tests`). No route, header or setting can skip the model check or force a verdict; the always-MATCH stub exists only in `tests/validator_fakes.py`. Uses a different model vendor from the extractor (currently DeepSeek `deepseek-flash`). Runs on its own Docker network, so the agent cannot reach it.

**Telemetry (Langfuse, via `shared/telemetry`).** Each authenticated `POST /internal/validate` is one `semantic_validation` workflow span that continues the gateway's trace from a validated `traceparent` header (correlation only, never authentication), with a nested `validator-llm-call` generation: model, provider, prompt version (`validator-v<N>` from the prompt's first line) and `PROMPT_HASH`, usage when reported (otherwise `usage_status=unavailable`), latency and the verdict as `validator_verdict`. The generation input is the data the model sees with account IDs replaced by their display labels; the system prompt is identified by hash only and the service token never reaches telemetry. Refusals (422/413) are traced as `rejected` without a generation; 401s are not traced. No generation is recorded when no model is configured. Without Langfuse keys the spans are no-ops; `create_app(telemetry=...)` lets tests inject `Telemetry.for_testing`. Telemetry never changes a result.

**Authenticated readiness.** `GET /health` reports liveness/configuration presence. `GET /internal/ready` uses the same constant-time bearer-token check as validation, then checks model key/model presence, without calling the model (200 `{status: "ready"}`; 401 `UNAUTHORIZED`; 503 `SERVICE_TOKEN_UNSET` or `LLM_NOT_CONFIGURED`). Gateway and validator must share the same `VALIDATOR_SERVICE_TOKEN` of at least 32 characters; restart both after changes. The gateway maps an authentication failure to `VALIDATOR_AUTH_FAILED` before extraction. This verifies service authentication, not provider access or credits; readiness contains no secret values.

`X-DCTA-Deadline` is honoured after authentication and caps the local model budget.
The executor checks that budget again after queue admission, and the provider receives
only the remaining time. Expired queued calls never invoke the model; late responses
become `LLM_TIMEOUT`, never MATCH. HTTP cancellation cannot guarantee that a remote
provider stops generating; the gateway independently discards stale/cancelled results.

Provider authentication/access errors and invalid model/request settings (HTTP
400/401/403/404/422 from the model SDK) become `LLM_CONFIGURATION_ERROR`; gateway
validation retries cannot repair them. Rate limits and provider/server transport
failures retain their transient error handling.
