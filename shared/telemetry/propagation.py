"""W3C ``traceparent`` propagation between DCTA services, and pseudonymous IDs.

A trace ID correlates telemetry only. It never authenticates a caller or selects a
user, session or conversation: authorization stays with the delegation token/session.

* Outgoing: :func:`inject_trace_headers` (or the httpx request hooks) writes the
  current span as ``traceparent``. Only ``traceparent`` is sent; Langfuse baggage is
  not propagated, so a caller cannot inject user/session attributes.
* Incoming: :func:`extract_trace_context` strictly validates the header and returns a
  :class:`TraceParent` or ``None``. Call it on internal service routes only; the
  gateway's browser-facing routes should start a fresh trace.
* :func:`pseudonymize` maps a user ID to ``u_<24 hex>`` with HMAC-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

__all__ = [
    "TRACEPARENT_HEADER",
    "TraceParent",
    "parse_traceparent",
    "extract_trace_context",
    "current_traceparent",
    "inject_trace_headers",
    "httpx_request_hook",
    "async_httpx_request_hook",
    "remote_parent",
    "pseudonymize",
    "PSEUDONYM_RE",
]

TRACEPARENT_HEADER = "traceparent"

# Version 00 only: exactly 55 lower-case characters.
_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


@dataclass(frozen=True)
class TraceParent:
    """A validated W3C trace context (version 00)."""

    trace_id: str
    span_id: str
    sampled: bool = True

    @property
    def header(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def parse_traceparent(value: Any) -> TraceParent | None:
    """Parse one header value; return None for anything malformed.

    Rejects: non-strings, other versions (including ``ff``), upper-case hex, all-zero
    IDs, extra fields, repeated/comma-joined values and over-long input.
    """
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _TRACEPARENT_RE.match(value.strip(" \t"))
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return TraceParent(trace_id=trace_id, span_id=span_id, sampled=bool(int(flags, 16) & 1))


def extract_trace_context(headers: Mapping[str, Any] | None) -> TraceParent | None:
    """Find and validate ``traceparent`` in incoming headers (case-insensitive)."""
    if not headers:
        return None
    try:
        values = [v for k, v in headers.items() if str(k).lower() == TRACEPARENT_HEADER]
    except Exception:  # noqa: BLE001 - odd header container: treat as absent
        return None
    if len(values) != 1:
        return None
    return parse_traceparent(values[0])


def current_traceparent() -> TraceParent | None:
    """The active span as a TraceParent, or None when no valid span is active."""
    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return TraceParent(
        trace_id=f"{span_context.trace_id:032x}",
        span_id=f"{span_context.span_id:016x}",
        sampled=bool(span_context.trace_flags & TraceFlags.SAMPLED),
    )


def inject_trace_headers(headers: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Return *headers* plus ``traceparent`` for the active span (unchanged if none).

    A dict copy is returned; the input mapping is updated in place too when given.
    """
    out: dict[str, str] = dict(headers or {})
    parent = current_traceparent()
    if parent is not None:
        out[TRACEPARENT_HEADER] = parent.header
        if headers is not None:
            with suppress(Exception):  # read-only mapping: the returned copy still has it
                headers[TRACEPARENT_HEADER] = parent.header
    return out


def httpx_request_hook(request: Any) -> None:
    """``httpx.Client(event_hooks={"request": [httpx_request_hook]})``."""
    try:
        parent = current_traceparent()
        if parent is not None:
            request.headers[TRACEPARENT_HEADER] = parent.header
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        pass


async def async_httpx_request_hook(request: Any) -> None:
    """``httpx.AsyncClient(event_hooks={"request": [async_httpx_request_hook]})``."""
    httpx_request_hook(request)


@contextmanager
def remote_parent(parent: TraceParent) -> Iterator[None]:
    """Make *parent* the current (remote) span context for the block.

    The sampled flag is forced on: an incoming header cannot switch tracing off.
    """
    span_context = SpanContext(
        trace_id=int(parent.trace_id, 16),
        span_id=int(parent.span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    token = otel_context.attach(otel_trace.set_span_in_context(NonRecordingSpan(span_context)))
    try:
        yield
    finally:
        otel_context.detach(token)


# ── Pseudonymous user IDs ────────────────────────────────────────────

#: Prototype pseudonym key context. It is public (in the repo), so a guessable user ID
#: could be confirmed by recomputing the HMAC. Production would use a secret key held
#: in a KMS and shared by all services so that pseudonyms still match across services.
_PSEUDONYM_CONTEXT = b"DCTA:telemetry:user-pseudonym:v1"
PSEUDONYM_RE = re.compile(r"^u_[0-9a-f]{24}$")


def pseudonymize(user_id: str, key: str | bytes | None = None) -> str:
    """Stable pseudonym ``u_<24 hex>`` = HMAC-SHA256(context [+ key], user_id)."""
    extra = key.encode() if isinstance(key, str) else (key or b"")
    digest = hmac.new(_PSEUDONYM_CONTEXT + b"\x00" + extra, str(user_id).encode(), hashlib.sha256)
    return "u_" + digest.hexdigest()[:24]
