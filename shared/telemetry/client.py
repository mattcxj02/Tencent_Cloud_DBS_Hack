"""The :class:`Telemetry` facade: one per service process, backed by Langfuse.

Design rules (CODEBUDDY "Langfuse and audit handling"):

* Nothing here raises into callers or changes a business result. Setup/teardown
  failures become a no-op span plus degraded health; the caller's own exceptions
  propagate unchanged.
* Only explicit, sanitized captures: payloads pass :func:`redaction.bounded_payload`,
  attributes pass :func:`redaction.sanitize_attributes`. No decorators, no argument
  capture, no auto-instrumentation (``should_export_span`` keeps Langfuse spans only).
* Exceptions are recorded as a status (``error: ValueError``), never with their
  message or stack trace; expected outcomes (clarification, blocked, timeout,
  rejected) are levels/status messages, not crashes.
* Spans are context managers: they close when the block exits, so none stays open
  while a person decides. Use :meth:`Telemetry.event` to mark "waiting for approval".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse import __version__ as _LANGFUSE_VERSION
from langfuse.span_filter import is_langfuse_span
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from .export import GuardedExporter, HealthState, make_mask_otel_spans
from .propagation import PSEUDONYM_RE, TraceParent, parse_traceparent, pseudonymize, remote_parent
from .redaction import (
    STATUS_LEVELS,
    WORKFLOW_STAGES,
    bounded_payload,
    redact,
    redact_text,
    safe_code,
    safe_name,
    safe_reason,
    sanitize_attributes,
)

__all__ = ["Telemetry", "ModelPrice", "SpanHandle", "GenerationHandle", "ReadBack"]

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL_PARAM_KEYS = frozenset(
    {"temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "response_format"}
)
_USAGE_KEYS = {
    "prompt_tokens": "input",
    "input_tokens": "input",
    "input": "input",
    "completion_tokens": "output",
    "output_tokens": "output",
    "output": "output",
    "total_tokens": "total",
    "total": "total",
    "reasoning_tokens": "output_reasoning",
    "cached_tokens": "input_cached",
}
_MAX_USAGE = 100_000_000


# ── Public value types ───────────────────────────────────────────────


@dataclass(frozen=True)
class ModelPrice:
    """Explicitly configured USD price per million tokens. Without one, cost is
    recorded as unavailable, never as zero."""

    input_per_million: Decimal
    output_per_million: Decimal


@dataclass
class ReadBack:
    """Result of reading a trace back from the Langfuse v2 observations API."""

    observations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class _Config:
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str


def _reason(value: Any) -> str | None:
    return safe_reason(value)


def _status(value: Any) -> str:
    return value if isinstance(value, str) and value in STATUS_LEVELS else "unknown"


def normalize_usage(usage: Any) -> dict[str, int] | None:
    """Map OpenAI-style or Langfuse-style usage to Langfuse usage_details (ints only)."""
    if usage is None:
        return None
    if not isinstance(usage, Mapping):
        dump = getattr(usage, "model_dump", None)
        usage = dump() if callable(dump) else None
        if not isinstance(usage, Mapping):
            return None
    out: dict[str, int] = {}
    flat: dict[str, Any] = dict(usage)
    for nested in ("completion_tokens_details", "prompt_tokens_details"):
        if isinstance(usage.get(nested), Mapping):
            flat.update(usage[nested])
    for key, value in flat.items():
        target = _USAGE_KEYS.get(str(key))
        if target is None or isinstance(value, bool) or not isinstance(value, int):
            continue
        if 0 <= value <= _MAX_USAGE:
            out[target] = value
    if "total" not in out and "input" in out and "output" in out:
        out["total"] = out["input"] + out["output"]
    return out or None


# ── Span handles ─────────────────────────────────────────────────────


class SpanHandle:
    """Handle yielded by :meth:`Telemetry.workflow_span`. Methods never raise."""

    def __init__(self, telemetry: Telemetry, observation: Any) -> None:
        self._telemetry = telemetry
        self._obs = observation
        self._started = time.monotonic()
        self._status: str | None = None
        self._reason: str | None = None
        self._meta: dict[str, Any] = {}
        self._dropped = 0
        self._output: Any = None
        self._has_output = False

    @property
    def trace_id(self) -> str | None:
        """32-hex trace ID to persist in audit rows (None when not exported)."""
        value = getattr(self._obs, "trace_id", None)
        return value if isinstance(value, str) and _TRACE_ID_RE.match(value) else None

    @property
    def observation_id(self) -> str | None:
        value = getattr(self._obs, "id", None)
        return value if isinstance(value, str) and re.match(r"^[0-9a-f]{16}$", value) else None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        try:
            clean, dropped = sanitize_attributes(attributes)
            self._meta.update(clean)
            self._dropped += dropped
        except Exception as exc:  # noqa: BLE001
            self._telemetry._health.note_internal("set_attributes", exc)

    def set_output(self, output: Any) -> None:
        self._output, self._has_output = output, True

    def set_status(self, status: str, *, reason_code: str | None = None) -> None:
        """Record an outcome: ok, pending, clarification, superseded (DEFAULT level);
        blocked, rejected, timeout, expired, cancelled (WARNING); error (ERROR)."""
        self._status = _status(status)
        self._reason = _reason(reason_code)

    # ── finishing (called by Telemetry; never raises) ──

    def _exception_status(self, exc: BaseException) -> tuple[str, str | None]:
        declared = getattr(exc, "telemetry_status", None)
        reason = _reason(getattr(exc, "reason_code", None)) or _reason(getattr(exc, "code", None))
        if isinstance(declared, str) and declared in STATUS_LEVELS:
            return declared, reason
        name = type(exc).__name__
        if name in {"CancelledError", "KeyboardInterrupt", "GeneratorExit"}:
            return "cancelled", reason
        if isinstance(exc, TimeoutError) or "Timeout" in name:
            return "timeout", reason
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and 400 <= status_code < 500:
            return "rejected", reason or f"HTTP_{status_code}"
        return "error", reason

    def _final_status(self, exc: BaseException | None) -> tuple[str, str | None]:
        if exc is None:
            return self._status or "ok", self._reason
        status, reason = self._exception_status(exc)
        if self._status not in (None, "ok"):  # an explicit non-ok outcome wins
            return self._status, self._reason or reason
        return status, reason

    def _finish_kwargs(self, exc: BaseException | None) -> dict[str, Any]:
        status, reason = self._final_status(exc)
        meta = dict(self._meta)
        meta["outcome"] = status
        if reason:
            meta["reason_code"] = reason
        if exc is not None:
            meta["error_type"] = safe_code(type(exc).__name__) or "Exception"
        if self._dropped:
            meta["dropped_attribute_count"] = self._dropped
        if time.monotonic() - self._started > self._telemetry._long_open_s:
            meta["long_open"] = True
        kwargs: dict[str, Any] = {"metadata": meta}
        if status != "ok":
            kwargs["level"] = STATUS_LEVELS[status]
            kwargs["status_message"] = f"{status}: {reason}" if reason else status
            if exc is not None and status == "error":
                kwargs["status_message"] = f"error: {meta['error_type']}"
        if self._has_output:
            payload, truncated = bounded_payload(self._output, self._telemetry._max_field_chars)
            kwargs["output"] = payload
            if truncated:
                meta["output_truncated"] = True
        return kwargs

    def _finish(self, exc: BaseException | None) -> None:
        try:
            self._obs.update(**self._finish_kwargs(exc))
        except Exception as err:  # noqa: BLE001
            self._telemetry._health.note_internal("span finish", err)


class GenerationHandle(SpanHandle):
    """Handle yielded by :meth:`Telemetry.generation`."""

    def __init__(self, telemetry: Telemetry, observation: Any, model: str) -> None:
        super().__init__(telemetry, observation)
        self._model = model
        self._usage: Any = None
        self._latency_ms: float | None = None

    def update(
        self,
        *,
        output: Any = None,
        usage: Any = None,
        latency_ms: float | None = None,
        status: str | None = None,
        reason_code: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if output is not None:
            self.set_output(output)
        if usage is not None:
            self._usage = usage
        if latency_ms is not None:
            self._latency_ms = latency_ms
        if status is not None:
            self.set_status(status, reason_code=reason_code)
        if attributes:
            self.set_attributes(attributes)

    def _finish_kwargs(self, exc: BaseException | None) -> dict[str, Any]:
        kwargs = super()._finish_kwargs(exc)
        meta = kwargs["metadata"]
        usage = normalize_usage(self._usage)
        meta["usage_status"] = "reported" if usage else "unavailable"
        if usage:
            kwargs["usage_details"] = usage
        cost, reason = self._telemetry._cost(self._model, usage)
        if cost is not None:
            kwargs["cost_details"] = cost
            meta["cost_status"] = "computed"
            meta["cost_source"] = "configured_price"
        else:
            meta["cost_status"] = "unavailable"
            meta["cost_unavailable_reason"] = reason
        latency, _ = sanitize_attributes({"latency_ms": self._latency_ms})
        meta.update(latency)
        return kwargs


class _NoopSpan(GenerationHandle):
    """Yielded when telemetry is disabled or a span could not start."""

    def __init__(self) -> None:  # deliberately skips SpanHandle.__init__
        self._obs = None

    trace_id = None  # type: ignore[assignment]
    observation_id = None  # type: ignore[assignment]

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def set_output(self, output: Any) -> None:
        return None

    def set_status(self, status: str, *, reason_code: str | None = None) -> None:
        return None

    def update(self, **_: Any) -> None:
        return None

    def _finish(self, exc: BaseException | None) -> None:
        return None


NOOP_SPAN = _NoopSpan()


# ── Backend registry (one Langfuse pipeline per public key per process) ──


def _otlp_exporter(cfg: Any, timeout_s: int) -> SpanExporter:
    """The same OTLP/HTTP endpoint and headers the Langfuse SDK uses by default."""
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    basic = base64.b64encode(
        f"{cfg.langfuse_public_key}:{cfg.langfuse_secret_key}".encode()
    ).decode("ascii")
    return OTLPSpanExporter(
        endpoint=f"{cfg.langfuse_base_url.rstrip('/')}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {basic}",
            "x-langfuse-sdk-name": "python",
            "x-langfuse-sdk-version": _LANGFUSE_VERSION,
            "x-langfuse-public-key": cfg.langfuse_public_key,
        },
        timeout=timeout_s,
        compression=Compression.Gzip,
    )


def _mask(*, data: Any, **_: Any) -> Any:
    """Langfuse ``mask`` hook: a second redaction pass over input/output/metadata."""
    return redact(data)


class _Backend:
    def __init__(self, cfg: Any, *, service: str, options: dict[str, Any]) -> None:
        self.public_key: str = cfg.langfuse_public_key
        self.base_url: str = cfg.langfuse_base_url.rstrip("/")
        self.health = HealthState()
        delegate = options["exporter"] or _otlp_exporter(cfg, options["timeout_s"])
        self.guard = GuardedExporter(
            delegate, self.health, max_pending_spans=options["max_pending_spans"]
        )
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": f"dcta-{service}"})
        )
        self.client = Langfuse(
            public_key=cfg.langfuse_public_key,
            secret_key=cfg.langfuse_secret_key,
            base_url=self.base_url,
            timeout=options["timeout_s"],
            flush_at=options["flush_at"],
            flush_interval=options["flush_interval_s"],
            environment=options["environment"],
            mask=_mask,
            mask_otel_spans=make_mask_otel_spans(self.health),
            should_export_span=is_langfuse_span,
            tracer_provider=self.provider,
            span_exporter=self.guard,
        )
        if not getattr(self.client, "_tracing_enabled", True):
            self.health.note_fatal("Langfuse tracing disabled by LANGFUSE_TRACING_ENABLED")
        self.refs = 0

    def close(self) -> None:
        try:
            self.client.flush()
        except Exception as exc:  # noqa: BLE001
            self.health.note_internal("flush", exc)
        try:
            self.client.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.health.note_internal("shutdown", exc)
        # Langfuse keeps a per-public-key singleton; drop it so a later Telemetry with
        # the same key gets a fresh pipeline instead of a shut-down one (private API).
        try:
            from langfuse._client.resource_manager import LangfuseResourceManager

            with LangfuseResourceManager._lock:
                LangfuseResourceManager._instances.pop(self.public_key, None)
        except Exception:  # noqa: BLE001
            pass


_BACKENDS: dict[str, _Backend] = {}
_BACKENDS_LOCK = threading.Lock()


# ── Telemetry facade ─────────────────────────────────────────────────


class Telemetry:
    """Safe Langfuse telemetry for one service.

    ``settings`` is any object with ``langfuse_public_key``, ``langfuse_secret_key`` and
    ``langfuse_base_url`` (e.g. ``AgentSettings``). Missing values → ``disabled`` health
    and no-op spans; nothing pretends to export.
    """

    def __init__(
        self,
        settings: Any,
        service: str,
        *,
        environment: str | None = None,
        prices: Mapping[str, ModelPrice] | None = None,
        pseudonym_key: str | bytes | None = None,
        project_id: str | None = None,
        timeout_s: int = 5,
        flush_at: int | None = None,
        flush_interval_s: float | None = None,
        max_pending_spans: int = 1024,
        max_field_chars: int = 8000,
        long_open_s: float = 120.0,
        exporter: SpanExporter | None = None,
    ) -> None:
        self.service = safe_code(service) or "unknown"
        self._prices = dict(prices or {})
        self._pseudonym_key = pseudonym_key
        self._project_id = project_id if isinstance(project_id, str) else None
        self._max_field_chars = max(256, max_field_chars)
        self._long_open_s = long_open_s
        self._backend: _Backend | None = None
        self._closed = False
        cfg = _Config(
            langfuse_public_key=getattr(settings, "langfuse_public_key", None) or "",
            langfuse_secret_key=getattr(settings, "langfuse_secret_key", None) or "",
            langfuse_base_url=getattr(settings, "langfuse_base_url", None) or "",
        )
        if not (cfg.langfuse_public_key and cfg.langfuse_secret_key and cfg.langfuse_base_url):
            self._health = HealthState(
                disabled_reason="Langfuse not configured: LANGFUSE_PUBLIC_KEY, "
                "LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL are required"
            )
            return
        options = {
            "environment": environment,
            "timeout_s": timeout_s,
            "flush_at": flush_at,
            "flush_interval_s": flush_interval_s,
            "max_pending_spans": max_pending_spans,
            "exporter": exporter,
        }
        try:
            with _BACKENDS_LOCK:
                backend = _BACKENDS.get(cfg.langfuse_public_key)
                if backend is None:
                    backend = _Backend(cfg, service=self.service, options=options)
                    _BACKENDS[cfg.langfuse_public_key] = backend
                backend.refs += 1
            self._backend = backend
            self._health = backend.health
        except Exception as exc:  # noqa: BLE001
            self._health = HealthState()
            self._health.note_fatal(f"Langfuse client initialisation failed: {type(exc).__name__}")

    @classmethod
    def for_testing(
        cls, service: str = "test", *, exporter: SpanExporter | None = None, **kwargs: Any
    ) -> Telemetry:
        """A Telemetry whose spans go to an in-memory exporter (tests only).

        Uses a unique fake key and an unroutable URL, so nothing leaves the process.
        The exporter is available as :attr:`exporter`.
        """
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        cfg = _Config(
            langfuse_public_key=f"pk-lf-test-{uuid.uuid4().hex}",
            langfuse_secret_key="sk-lf-test-placeholder",
            langfuse_base_url="http://langfuse.invalid",
        )
        kwargs.setdefault("project_id", "test-project")
        return cls(cfg, service, exporter=exporter or InMemorySpanExporter(), **kwargs)

    # ── status ──

    @property
    def enabled(self) -> bool:
        return self._backend is not None and not self._closed and self._health.fatal_error is None

    @property
    def exporter(self) -> SpanExporter | None:
        """The delegate exporter (the in-memory one in tests)."""
        return self._backend.guard.delegate if self._backend else None

    def health(self) -> dict[str, Any]:
        """``{"backend", "state": ok|degraded|disabled, "last_error", "dropped", ...}``."""
        snapshot = self._health.snapshot()
        snapshot["service"] = self.service
        if self._closed:
            snapshot["state"] = "disabled"
            snapshot["last_error"] = "telemetry shut down"
        return snapshot

    def check_backend(self) -> bool:
        """Call Langfuse ``auth_check`` (network). Updates health; never raises."""
        if not self.enabled:
            return False
        try:
            ok = bool(self._backend.client.auth_check())  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._health.note_failure(f"Langfuse auth_check raised {type(exc).__name__}")
            return False
        if ok:
            self._health.note_success(0)
        else:
            self._health.note_failure("Langfuse auth_check failed (keys or base URL)")
        return ok

    # ── identifiers ──

    def pseudonymize(self, user_id: str) -> str:
        return pseudonymize(user_id, self._pseudonym_key)

    def _user(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and PSEUDONYM_RE.match(value):
            return value
        return self.pseudonymize(str(value))  # never export a raw user ID

    def _session(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if _SESSION_RE.match(text) and redact_text(text) == text:
            return text
        key = b"DCTA:telemetry:session:v1"
        return "s_" + hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:24]

    @staticmethod
    def current_trace_id() -> str | None:
        ctx = otel_trace.get_current_span().get_span_context()
        return f"{ctx.trace_id:032x}" if ctx.is_valid else None

    @staticmethod
    def current_observation_id() -> str | None:
        ctx = otel_trace.get_current_span().get_span_context()
        return f"{ctx.span_id:016x}" if ctx.is_valid else None

    # ── spans ──

    @contextmanager
    def workflow_span(
        self,
        name: str,
        *,
        session_id: str | None = None,
        user_pseudonym: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        input: Any = None,
        parent: TraceParent | str | None = None,
    ) -> Iterator[SpanHandle]:
        """One workflow stage. ``parent`` continues a trace from a validated incoming
        ``traceparent`` (ignored when a span is already active in this process)."""
        with self._observation(
            name,
            as_type="span",
            session_id=session_id,
            user_pseudonym=user_pseudonym,
            attributes=attributes,
            input=input,
            parent=parent,
            extra={},
            make_handle=lambda obs: SpanHandle(self, obs),
        ) as handle:
            yield handle

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        prompt_version: str,
        prompt_hash: str,
        input: Any = None,
        output: Any = None,
        usage: Any = None,
        latency_ms: float | None = None,
        provider: str | None = None,
        model_parameters: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        user_pseudonym: str | None = None,
        parent: TraceParent | str | None = None,
    ) -> Iterator[GenerationHandle]:
        """One LLM call. Usage/latency/output may be given here or via ``handle.update``."""
        safe_model = safe_code(model) or "unknown"
        meta: dict[str, Any] = dict(attributes or {})
        meta.update(model=safe_model, prompt_version=prompt_version, prompt_hash=prompt_hash)
        if provider is not None:
            meta["provider"] = provider
        extra = {"model": safe_model, "model_parameters": _model_params(model_parameters)}

        def make(obs: Any) -> GenerationHandle:
            handle = GenerationHandle(self, obs, safe_model)
            handle.update(output=output, usage=usage, latency_ms=latency_ms)
            return handle

        with self._observation(
            name,
            as_type="generation",
            session_id=session_id,
            user_pseudonym=user_pseudonym,
            attributes=meta,
            input=input,
            parent=parent,
            extra=extra,
            make_handle=make,
        ) as handle:
            yield handle  # type: ignore[misc]

    def event(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        status: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """A zero-duration marker (e.g. ``awaiting_approval``) under the current span."""
        if not self.enabled:
            return
        try:
            meta, dropped = sanitize_attributes(attributes)
            meta["service"] = self.service
            if dropped:
                meta["dropped_attribute_count"] = dropped
            kwargs: dict[str, Any] = {"name": safe_name(name), "metadata": meta}
            if status is not None:
                state, reason = _status(status), _reason(reason_code)
                meta["outcome"] = state
                if reason:
                    meta["reason_code"] = reason
                if state != "ok":
                    kwargs["level"] = STATUS_LEVELS[state]
                    kwargs["status_message"] = f"{state}: {reason}" if reason else state
            self._backend.client.create_event(**kwargs)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._health.note_internal("event", exc, dropped=1)

    @contextmanager
    def _observation(
        self,
        name: str,
        *,
        as_type: str,
        session_id: Any,
        user_pseudonym: Any,
        attributes: Mapping[str, Any] | None,
        input: Any,
        parent: Any,
        extra: dict[str, Any],
        make_handle: Any,
    ) -> Iterator[SpanHandle]:
        if not self.enabled:
            yield NOOP_SPAN
            return
        stack: ExitStack | None = ExitStack()
        handle: SpanHandle = NOOP_SPAN
        try:
            obs, dropped = self._start(
                stack,  # type: ignore[arg-type]
                name,
                as_type=as_type,
                session_id=session_id,
                user_pseudonym=user_pseudonym,
                attributes=attributes,
                input=input,
                parent=parent,
                extra=extra,
            )
            handle = make_handle(obs)
            handle._dropped += dropped
        except Exception as exc:  # noqa: BLE001
            self._health.note_internal("span start", exc, dropped=1)
            self._close(stack)
            stack = None
            handle = NOOP_SPAN
        try:
            yield handle
        except BaseException as exc:
            if stack is not None:
                handle._finish(exc)
                self._close(stack)
            raise
        else:
            if stack is not None:
                handle._finish(None)
                self._close(stack)

    def _start(
        self,
        stack: ExitStack,
        name: str,
        *,
        as_type: str,
        session_id: Any,
        user_pseudonym: Any,
        attributes: Mapping[str, Any] | None,
        input: Any,
        parent: Any,
        extra: dict[str, Any],
    ) -> tuple[Any, int]:
        if isinstance(parent, str):
            parent = parse_traceparent(parent)
        if (
            isinstance(parent, TraceParent)
            and not otel_trace.get_current_span().get_span_context().is_valid
        ):
            stack.enter_context(remote_parent(parent))
        user, session = self._user(user_pseudonym), self._session(session_id)
        if user or session:
            stack.enter_context(propagate_attributes(user_id=user, session_id=session))
        meta, dropped = sanitize_attributes(attributes)
        span_name = safe_name(name)
        meta["service"] = self.service
        if span_name in WORKFLOW_STAGES:
            meta["stage"] = span_name
        if dropped:
            meta["dropped_attribute_count"] = dropped
        payload, truncated = bounded_payload(input, self._max_field_chars)
        if truncated:
            meta["input_truncated"] = True
        obs = stack.enter_context(
            self._backend.client.start_as_current_observation(  # type: ignore[union-attr]
                name=span_name, as_type=as_type, input=payload, metadata=meta, **extra
            )
        )
        return obs, dropped

    def _close(self, stack: ExitStack) -> None:
        try:
            stack.close()
        except Exception as exc:  # noqa: BLE001
            self._health.note_internal("span end", exc)

    def _cost(
        self, model: str, usage: dict[str, int] | None
    ) -> tuple[dict[str, float] | None, str]:
        price = self._prices.get(model)
        if price is None:
            return None, "no_configured_price"
        if not usage or "input" not in usage or "output" not in usage:
            return None, "usage_missing"
        try:
            million = Decimal(1_000_000)
            cost_in = Decimal(usage["input"]) * Decimal(price.input_per_million) / million
            cost_out = Decimal(usage["output"]) * Decimal(price.output_per_million) / million
        except (InvalidOperation, TypeError, ValueError):
            return None, "invalid_price"
        return {
            "input": float(cost_in),
            "output": float(cost_out),
            "total": float(cost_in + cost_out),
        }, ""

    # ── links, read-back, lifecycle ──

    def trace_url(self, trace_id: str | None) -> str | None:
        """Langfuse UI link for *trace_id* (None if disabled/invalid/unknown project)."""
        if not self._backend or not isinstance(trace_id, str) or not _TRACE_ID_RE.match(trace_id):
            return None
        if self._project_id:
            return f"{self._backend.base_url}/project/{self._project_id}/traces/{trace_id}"
        try:
            return self._backend.client.get_trace_url(trace_id=trace_id)
        except Exception:  # noqa: BLE001 - a missing link is not a telemetry failure
            return None

    def read_back(
        self,
        trace_id: str,
        *,
        wait_s: float = 0.0,
        poll_interval_s: float = 3.0,
        min_count: int = 1,
        fields: str = "core,basic,io,metadata,model,usage",
        window: timedelta = timedelta(hours=2),
    ) -> ReadBack:
        """Read a trace's observations via ``GET /api/public/v2/observations`` (the legacy
        ``/traces/{id}`` API returns 410 for this org). Polls up to ``wait_s`` for
        ingestion lag. For evidence and tests; never raises."""
        if not self._backend or not isinstance(trace_id, str) or not _TRACE_ID_RE.match(trace_id):
            return ReadBack(error="telemetry disabled or invalid trace id")
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            observations, error = self._read_once(trace_id, fields, window)
            if len(observations) >= min_count or time.monotonic() >= deadline:
                return ReadBack(observations=observations, error=error)
            time.sleep(max(0.5, poll_interval_s))

    def _read_once(
        self, trace_id: str, fields: str, window: timedelta
    ) -> tuple[list[dict[str, Any]], str | None]:
        now = datetime.now(UTC)
        try:
            response = self._backend.client.api.observations.get_many(  # type: ignore[union-attr]
                trace_id=trace_id,
                from_start_time=now - window,
                to_start_time=now + timedelta(minutes=10),
                fields=fields,
                limit=100,
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "status_code", None)
            suffix = f" (HTTP {code})" if isinstance(code, int) else ""
            return [], f"read-back failed: {type(exc).__name__}{suffix}"
        rows = []
        for item in response.data:
            dump = getattr(item, "model_dump", None)
            rows.append(dump(mode="json", by_alias=True) if callable(dump) else dict(item))
        return rows, None

    def flush(self) -> None:
        """Export everything queued, then retry buffered batches. Never raises."""
        if not self._backend:
            return
        try:
            self._backend.client.flush()
        except Exception as exc:  # noqa: BLE001
            self._health.note_internal("flush", exc)
        self._backend.guard.reexport_pending()

    def reexport_pending(self) -> int:
        """Retry sanitized batches that failed to export (bounded buffer). Returns the
        number of spans delivered. Only telemetry is re-sent; no business operation is
        ever replayed to recreate a trace."""
        return self._backend.guard.reexport_pending() if self._backend else 0

    def shutdown(self) -> None:
        """Flush and release the backend (call from the service's lifespan shutdown)."""
        if self._closed:
            return
        self._closed = True
        backend = self._backend
        if backend is None:
            return
        self.flush()
        with _BACKENDS_LOCK:
            backend.refs -= 1
            last = backend.refs <= 0
            if last and _BACKENDS.get(backend.public_key) is backend:
                _BACKENDS.pop(backend.public_key, None)
        if last:
            backend.close()


def _model_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key not in _MODEL_PARAM_KEYS:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, Mapping) and isinstance(value.get("type"), str):
            out[key] = safe_code(value["type"]) or "other"  # response_format={"type": ...}
        elif isinstance(value, str):
            code = safe_code(value)
            if code:
                out[key] = code
    return out or None
