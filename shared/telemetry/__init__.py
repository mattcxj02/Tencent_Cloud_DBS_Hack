"""Safe Langfuse telemetry shared by gateway, agent and validator (T-08).

Typical use in a service::

    from shared.telemetry import Telemetry, extract_trace_context, httpx_request_hook

    telemetry = Telemetry(load_agent_settings(), "agent")       # once per process
    with telemetry.workflow_span("extract", session_id=sid,
                                 user_pseudonym=telemetry.pseudonymize(user_id),
                                 parent=extract_trace_context(request.headers)) as span:
        ...
        span.set_status("clarification", reason_code="AMBIGUOUS_PAYEE")
    telemetry.shutdown()                                         # lifespan shutdown

See ``shared/telemetry/README.md`` for the allowlist and redaction rules.
"""

from .client import GenerationHandle, ModelPrice, ReadBack, SpanHandle, Telemetry
from .propagation import (
    TRACEPARENT_HEADER,
    TraceParent,
    async_httpx_request_hook,
    current_traceparent,
    extract_trace_context,
    httpx_request_hook,
    inject_trace_headers,
    parse_traceparent,
    pseudonymize,
)
from .redaction import (
    ALLOWED_ATTRIBUTES,
    STATUS_LEVELS,
    WORKFLOW_STAGES,
    redact,
    redact_text,
    sanitize_attributes,
)

__all__ = [
    "Telemetry",
    "SpanHandle",
    "GenerationHandle",
    "ModelPrice",
    "ReadBack",
    "TraceParent",
    "TRACEPARENT_HEADER",
    "parse_traceparent",
    "extract_trace_context",
    "current_traceparent",
    "inject_trace_headers",
    "httpx_request_hook",
    "async_httpx_request_hook",
    "pseudonymize",
    "ALLOWED_ATTRIBUTES",
    "STATUS_LEVELS",
    "WORKFLOW_STAGES",
    "redact",
    "redact_text",
    "sanitize_attributes",
]
