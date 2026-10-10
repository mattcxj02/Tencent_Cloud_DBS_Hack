"""Langfuse observations for the agent, through the T-08 ``shared.telemetry`` facade.

One ``agent.run`` span per request (continuing the gateway's ``traceparent``), one span
per workflow stage, and one ``extractor`` generation with model, prompt version/hash,
usage and latency. The system prompt, reasoning tokens, tokens and headers are never
passed in; the facade redacts and allowlists everything again before export. Telemetry
is optional (``None`` → no-ops) and can never change the workflow's outcome.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.telemetry import Telemetry

__all__ = ["NULL_HANDLE", "current_ids", "generation", "record_outcome", "record_update", "span"]


class _NullHandle:
    trace_id = None
    observation_id = None

    def set_status(self, *_: Any, **__: Any) -> None:
        return None

    def set_attributes(self, *_: Any, **__: Any) -> None:
        return None

    def update(self, **_: Any) -> None:
        return None


NULL_HANDLE = _NullHandle()


def span(telemetry: Telemetry | None, name: str, **kwargs: Any) -> AbstractContextManager[Any]:
    if telemetry is None:
        return nullcontext(NULL_HANDLE)
    return telemetry.workflow_span(name, **kwargs)


def generation(
    telemetry: Telemetry | None, name: str, **kwargs: Any
) -> AbstractContextManager[Any]:
    if telemetry is None:
        return nullcontext(NULL_HANDLE)
    return telemetry.generation(name, **kwargs)


def current_ids(telemetry: Telemetry | None) -> tuple[str | None, str | None]:
    """Trace and observation IDs of the active span, for agent audit events."""
    if telemetry is None:
        return None, None
    return telemetry.current_trace_id(), telemetry.current_observation_id()


def record_update(handle: Any, update: dict[str, Any]) -> None:
    """Set a stage span's status from the node's state update."""
    issue = update.get("issue")
    outcome = update.get("outcome") or {}
    if update.get("error"):
        handle.set_status("error", reason_code=update["error"])
    elif issue is not None:
        handle.set_status("clarification", reason_code=issue.code)
    elif outcome:
        record_outcome(handle, outcome)
    if update.get("plans"):
        handle.set_attributes({"leg_count": len(update["plans"])})


def record_outcome(handle: Any, outcome: dict[str, Any]) -> None:
    status = outcome.get("status")
    if status == "clarify":
        handle.set_status("clarification", reason_code=outcome.get("reason"))
    elif status == "draft_frozen":
        attributes = {"bundle_id": outcome.get("bundle_id"), "leg_count": len(outcome["legs"])}
        if outcome.get("bundle_hash"):
            attributes["bundle_hash"] = outcome["bundle_hash"]
        handle.set_attributes(attributes)
    elif status == "error":
        handle.set_status("rejected", reason_code=outcome.get("code"))
