"""The gateway's Langfuse telemetry (T-08): one facade per process, plus small helpers.

``get_gateway_telemetry()`` builds the single :class:`shared.telemetry.Telemetry` for the
gateway from ``GatewaySettings`` (disabled, never faked, without Langfuse keys). Tests
replace it with ``_set_gateway_telemetry`` (a disabled facade or
``Telemetry.for_testing``); that hook is module-level and unreachable over HTTP.

Workflow stages recorded by the gateway (each a context-managed span, so none stays open
while a person decides): ``ingest`` (user turns), ``gateway.draft`` / ``agent.call``
(orchestration), ``compile`` (freezing the agent's proposal), ``semantic_validation``,
``challenge`` (envelope issuance), ``policy``, ``approval`` (assertion outcome),
``step_up``, ``execution`` and ``receipt``. Browser routes start a fresh trace whose
session ID is the conversation ID; internal routes continue a validated ``traceparent``.

Telemetry never changes an outcome: the facade swallows its own failures, and
:func:`stage` falls back to a no-op span if the facade cannot even be built.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from shared.settings import load_gateway_settings
from shared.telemetry import Telemetry, TraceParent
from shared.telemetry.client import NOOP_SPAN, SpanHandle

__all__ = [
    "get_gateway_telemetry",
    "stage",
    "event",
    "record_policy",
    "health",
    "trace_url",
    "trace_urls",
]

_telemetry: Telemetry | None = None
_lock = threading.Lock()

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
#: After a failed trace-link lookup, do not ask Langfuse again for this long.
_LINK_RETRY_SECONDS = 60.0
_link_cache: dict[int, tuple[str | None, float]] = {}


def get_gateway_telemetry() -> Telemetry:
    """The process-wide gateway facade (created lazily from gateway settings)."""
    global _telemetry  # noqa: PLW0603
    with _lock:
        if _telemetry is None:
            _telemetry = Telemetry(load_gateway_settings(), "gateway")
        return _telemetry


def _set_gateway_telemetry(telemetry: Telemetry | None) -> None:
    """Test hook: inject a disabled or in-memory facade (``None`` → rebuilt from settings)."""
    global _telemetry  # noqa: PLW0603
    with _lock:
        _telemetry = telemetry
        _link_cache.clear()


def _facade() -> Telemetry | None:
    try:
        return get_gateway_telemetry()
    except Exception:  # noqa: BLE001 - e.g. unreadable settings: no telemetry, same outcome
        return None


@contextmanager
def stage(
    name: str,
    *,
    user_id: str | None = None,
    conversation_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    parent: TraceParent | None = None,
) -> Iterator[SpanHandle]:
    """One gateway workflow span. ``user_id`` is pseudonymized; the session is the
    conversation. Nested stages inherit user/session from the enclosing span."""
    telemetry = _facade()
    if telemetry is None:
        yield NOOP_SPAN
        return
    user = telemetry.pseudonymize(user_id) if user_id else None
    with telemetry.workflow_span(
        name,
        session_id=conversation_id,
        user_pseudonym=user,
        attributes=attributes,
        parent=parent,
    ) as span:
        yield span


def event(name: str, *, attributes: Mapping[str, Any] | None = None) -> None:
    """A zero-duration marker under the current span (e.g. ``awaiting_approval``)."""
    telemetry = _facade()
    if telemetry is not None:
        telemetry.event(name, attributes=attributes)


def record_policy(span: SpanHandle, decision: str, codes: Iterable[str]) -> None:
    """Policy outcome on a ``policy`` span: BLOCK is an expected ``blocked`` status."""
    codes = tuple(codes)
    span.set_attributes(
        {"policy_decision": str(decision), "step_up_required": decision == "STEP_UP"}
    )
    first = codes[0] if codes else None
    if decision == "PASS":
        span.set_status("ok")
    elif decision == "STEP_UP":
        span.set_status("ok", reason_code=first or "STEP_UP_REQUIRED")
    else:
        span.set_status("blocked", reason_code=first or "POLICY_BLOCK")


def health() -> dict[str, Any]:
    """Telemetry health for ``GET /health`` (no secrets)."""
    telemetry = _facade()
    if telemetry is None:
        return {"backend": "langfuse", "state": "disabled", "last_error": "unavailable"}
    return telemetry.health()


def _link_prefix(telemetry: Telemetry) -> str | None:
    """``<base>/project/<id>/traces/`` from one lookup; failures are cached briefly so an
    evidence view never waits on Langfuse once per trace."""
    key = id(telemetry)
    cached = _link_cache.get(key)
    now = time.monotonic()
    if cached is not None and (cached[0] is not None or now - cached[1] < _LINK_RETRY_SECONDS):
        return cached[0]
    probe = "0" * 31 + "1"
    url = telemetry.trace_url(probe)
    prefix = url[: -len(probe)] if isinstance(url, str) and url.endswith(probe) else None
    _link_cache[key] = (prefix, now)
    return prefix


def trace_url(trace_id: str | None) -> str | None:
    """Owner-visible Langfuse link for *trace_id*, or None (disabled, invalid, unknown)."""
    return trace_urls([trace_id]).get(trace_id or "") if trace_id else None


def trace_urls(trace_ids: Iterable[str | None]) -> dict[str, str]:
    """Links for valid trace IDs (in first-seen order); empty when telemetry is off."""
    wanted = [t for t in dict.fromkeys(trace_ids) if isinstance(t, str) and _TRACE_ID_RE.match(t)]
    telemetry = _facade()
    if not wanted or telemetry is None or not telemetry.enabled:
        return {}
    try:
        prefix = _link_prefix(telemetry)
    except Exception:  # noqa: BLE001 - a missing link is not a failure
        return {}
    if prefix is None:
        return {}
    return {t: prefix + t for t in wanted}
