"""Export guard: final scrub, health accounting and a bounded re-export buffer.

Pipeline (one per Langfuse public key):

    BatchSpanProcessor (Langfuse, bounded queue)
      -> should_export_span: only spans from the Langfuse tracer (no auto-instrumentation)
      -> Langfuse media handling + ``mask_otel_spans`` (:func:`mask_otel_spans`)
      -> :class:`GuardedExporter` (drop events, scrub status/name, count, buffer)
      -> OTLP/HTTP exporter to Langfuse Cloud  (or an in-memory exporter in tests)

Failed batches are already sanitized, so keeping them for re-export never re-reads
business data and never replays a business operation.
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Sequence
from typing import Any

from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from .propagation import PSEUDONYM_RE
from .redaction import export_metadata_key_allowed, redact_text, safe_name

__all__ = ["HealthState", "GuardedExporter", "make_mask_otel_spans", "EXPORT_ATTRIBUTE_KEYS"]

#: Langfuse span attributes that may be exported. Metadata keys are checked separately.
EXPORT_ATTRIBUTE_KEYS = frozenset(
    {
        "langfuse.observation.type",
        "langfuse.observation.level",
        "langfuse.observation.status_message",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.observation.model.name",
        "langfuse.observation.model.parameters",
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "langfuse.observation.completion_start_time",
        "langfuse.version",
        "langfuse.environment",
        "langfuse.release",
        "langfuse.internal.as_root",
        "langfuse.internal.is_app_root",
        "user.id",
        "session.id",
    }
)
_METADATA_PREFIXES = ("langfuse.observation.metadata.", "langfuse.trace.metadata.")
_SESSION_MAX = 128


class HealthState:
    """Thread-safe export health shared by a Telemetry backend and its exporter."""

    def __init__(self, *, disabled_reason: str | None = None) -> None:
        self._lock = threading.Lock()
        self.disabled_reason = disabled_reason
        self.fatal_error: str | None = None
        self.last_error: str | None = disabled_reason
        self.last_error_at: float | None = None
        self.last_export_failed = False
        self.internal_error_since_success = False
        self.dropped = 0
        self.exported = 0
        self.pending = 0

    def note_success(self, count: int) -> None:
        with self._lock:
            self.exported += count
            self.last_export_failed = False
            self.internal_error_since_success = False

    def note_failure(self, error: str) -> None:
        with self._lock:
            self.last_export_failed = True
            self._set_error(error)

    def note_internal(self, where: str, exc: BaseException | None = None, dropped: int = 0) -> None:
        with self._lock:
            self.internal_error_since_success = True
            self.dropped += dropped
            detail = f": {type(exc).__name__}" if exc is not None else ""
            self._set_error(f"telemetry {where} failed{detail}")

    def note_dropped(self, count: int, reason: str) -> None:
        if count <= 0:
            return
        with self._lock:
            self.dropped += count
            self._set_error(reason)

    def note_fatal(self, error: str) -> None:
        with self._lock:
            self.fatal_error = error
            self._set_error(error)

    def set_pending(self, count: int) -> None:
        with self._lock:
            self.pending = count

    def _set_error(self, error: str) -> None:
        self.last_error = redact_text(error)[:300]
        self.last_error_at = time.time()

    @property
    def state(self) -> str:
        with self._lock:
            if self.disabled_reason:
                return "disabled"
            if (
                self.fatal_error
                or self.pending
                or self.last_export_failed
                or self.internal_error_since_success
            ):
                return "degraded"
            return "ok"

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        with self._lock:
            return {
                "backend": "langfuse",
                "state": state,
                "last_error": self.last_error,
                "dropped": self.dropped,
                "pending": self.pending,
                "exported": self.exported,
            }


# ── mask_otel_spans: export-time attribute allowlist ─────────────────


def _attribute_allowed(key: str, value: Any) -> bool:
    if key == "user.id":
        return isinstance(value, str) and bool(PSEUDONYM_RE.match(value))
    if key == "session.id":
        return isinstance(value, str) and 0 < len(value) <= _SESSION_MAX
    if key in EXPORT_ATTRIBUTE_KEYS:
        return True
    for prefix in _METADATA_PREFIXES:
        if key.startswith(prefix):
            return export_metadata_key_allowed(key[len(prefix) :])
    return False


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list | tuple):
        return tuple(redact_text(v) if isinstance(v, str) else v for v in value)
    return value


def make_mask_otel_spans(health: HealthState) -> Any:
    """Build the Langfuse ``mask_otel_spans`` hook: drop non-allowlisted attributes and
    re-redact every exported string. On an internal error the batch is dropped (fail
    closed) and counted."""

    def mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
        try:
            patches: dict[Any, OtelSpanPatch] = {}
            for identifier, span in params.spans.items():
                delete: list[str] = []
                updates: dict[str, Any] = {}
                for key, value in span.attributes.items():
                    if not _attribute_allowed(key, value):
                        delete.append(key)
                        continue
                    scrubbed = _scrub_value(value)
                    if scrubbed != value:
                        updates[key] = scrubbed
                if delete or updates:
                    patches[identifier] = OtelSpanPatch(
                        set_attributes=updates, delete_attributes=tuple(delete)
                    )
            return MaskOtelSpansResult(span_patches=patches)
        except Exception as exc:
            health.note_internal("export masking", exc, dropped=len(params.spans))
            raise

    return mask_otel_spans


# ── Guarded exporter ─────────────────────────────────────────────────


def _scrub_span(span: ReadableSpan) -> ReadableSpan:
    """Clone without events (exception messages/stack traces) and with a safe status."""
    status = span.status
    if status.description:
        status = Status(status.status_code, redact_text(status.description)[:200])
    elif status.status_code is StatusCode.UNSET:
        status = Status(StatusCode.UNSET)
    return ReadableSpan(
        name=safe_name(span.name, "redacted-span-name"),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes={k: v for k, v in (span.attributes or {}).items() if _attribute_allowed(k, v)},
        events=(),
        links=tuple(span.links or ()),
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class GuardedExporter(SpanExporter):
    """Wrap the real exporter: scrub, account in :class:`HealthState`, buffer failures.

    ``max_pending_spans`` bounds the re-export buffer; the oldest batches are dropped
    (and counted) when it overflows. The OTLP exporter already retries transient
    errors within its timeout; this buffer covers longer outages.
    """

    def __init__(
        self, delegate: SpanExporter, health: HealthState, *, max_pending_spans: int = 1024
    ) -> None:
        self.delegate = delegate
        self._health = health
        self._max_pending = max(0, max_pending_spans)
        self._pending: collections.deque[list[ReadableSpan]] = collections.deque()
        self._pending_count = 0
        self._lock = threading.RLock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            batch = [_scrub_span(s) for s in spans]
        except Exception as exc:  # noqa: BLE001 - fail closed: drop, count, degrade
            self._health.note_internal("export scrub", exc, dropped=len(spans))
            return SpanExportResult.FAILURE
        with self._lock:
            if self._send(batch):
                self._drain_locked()
                return SpanExportResult.SUCCESS
            self._buffer_locked(batch)
            return SpanExportResult.FAILURE

    def reexport_pending(self) -> int:
        """Retry buffered batches oldest-first; stop at the first failure. Never raises."""
        with self._lock:
            return self._drain_locked()

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count

    def shutdown(self) -> None:
        with self._lock:
            self._drain_locked()
            if self._pending_count:
                self._health.note_dropped(
                    self._pending_count, "telemetry shut down with unexported spans"
                )
                self._pending.clear()
                self._pending_count = 0
                self._health.set_pending(0)
        try:
            self.delegate.shutdown()
        except Exception as exc:  # noqa: BLE001
            self._health.note_internal("exporter shutdown", exc)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self.delegate.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001
            return False

    # ── internals (caller holds self._lock) ──

    def _send(self, batch: list[ReadableSpan]) -> bool:
        if not batch:
            return True
        try:
            result = self.delegate.export(batch)
        except Exception as exc:  # noqa: BLE001
            self._health.note_failure(f"Langfuse export raised {type(exc).__name__}")
            return False
        if result is SpanExportResult.SUCCESS:
            self._health.note_success(len(batch))
            return True
        self._health.note_failure("Langfuse export failed (OTLP export returned FAILURE)")
        return False

    def _buffer_locked(self, batch: list[ReadableSpan]) -> None:
        if len(batch) > self._max_pending:
            overflow = len(batch) - self._max_pending
            self._health.note_dropped(overflow, "telemetry re-export buffer full")
            batch = batch[overflow:]
        while self._pending and self._pending_count + len(batch) > self._max_pending:
            oldest = self._pending.popleft()
            self._pending_count -= len(oldest)
            self._health.note_dropped(len(oldest), "telemetry re-export buffer full")
        if batch:
            self._pending.append(batch)
            self._pending_count += len(batch)
        self._health.set_pending(self._pending_count)

    def _drain_locked(self) -> int:
        sent = 0
        while self._pending:
            batch = self._pending[0]
            if not self._send(batch):
                break
            self._pending.popleft()
            self._pending_count -= len(batch)
            sent += len(batch)
        self._health.set_pending(self._pending_count)
        return sent
