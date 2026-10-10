"""Validator service FastAPI application (internal, gateway-only).

The validator has no ledger access, no execution tools and no signing credentials. It
uses one LLM (semantic assessment) and, later, Langfuse (telemetry).

Routes:
- ``GET /health``: liveness plus configuration status; never shows secrets.
- ``POST /internal/validate``: requires ``Authorization: Bearer <VALIDATOR_SERVICE_TOKEN>``
  (constant-time compare). Without a configured token (>= 32 chars) every request is
  refused with 503 SERVICE_TOKEN_UNSET. The body is a ``ValidationRequest`` (strict,
  unknown fields rejected, <= 256 KiB, context budget enforced). Request problems return
  ``{"status": "ERROR", "code": ...}`` with HTTP 401/413/422/503 and no model call.
  Otherwise HTTP 200 with a ``ValidationResult`` (status OK or ERROR).

There is no route, header, query parameter or setting that skips the model check or
forces a verdict.

Telemetry (T-08 helpers, Langfuse): each authenticated validation is one
``semantic_validation`` workflow span, continuing the gateway's trace from a validated
``traceparent`` header, with a nested ``validator-llm-call`` generation (model, prompt
version and hash, usage, latency, verdict). The generation input is the data the model
sees with account IDs replaced by their display labels; the system prompt is identified
by hash only, and the service token never reaches telemetry. Without Langfuse keys the
spans are no-ops. Telemetry never changes a result.
"""

from __future__ import annotations

import hmac
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.clients.validator import (
    MAX_REQUEST_BYTES,
    VALIDATE_PATH,
    ErrorCode,
    ValidationRequest,
    ValidationResult,
    request_problem,
)
from shared.settings import ValidatorSettings, load_validator_settings
from shared.telemetry import Telemetry, TraceParent, extract_trace_context
from validator.llm import LLMClient, build_llm
from validator.prompt import PROMPT_HASH, SYSTEM_PROMPT, model_view
from validator.service import ValidatorService

__all__ = ["app", "create_app", "PROMPT_VERSION"]

_NO_STORE = {"Cache-Control": "no-store"}


def _prompt_version() -> str:
    """``validator-v<N>`` from the prompt's first line, e.g. "(version 1)"."""
    first_line = SYSTEM_PROMPT.splitlines()[0] if SYSTEM_PROMPT else ""
    match = re.search(r"\(version (\d+)\)", first_line)
    return f"validator-v{match.group(1)}" if match else "validator-unversioned"


#: Prompt version recorded with every generation (the hash identifies the exact text).
PROMPT_VERSION = _prompt_version()


def _refusal(status_code: int, code: ErrorCode) -> JSONResponse:
    headers = dict(_NO_STORE)
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"status": "ERROR", "code": code}, status_code=status_code, headers=headers)


def _token_refusal(settings: ValidatorSettings, authorization: str | None) -> JSONResponse | None:
    secret = settings.validator_service_token
    if not settings.service_token_configured or secret is None:
        return _refusal(503, "SERVICE_TOKEN_UNSET")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return _refusal(401, "UNAUTHORIZED")
    expected = secret.get_secret_value().encode("utf-8")
    if not hmac.compare_digest(presented.strip().encode("utf-8"), expected):
        return _refusal(401, "UNAUTHORIZED")
    return None


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read the body, or return None as soon as it exceeds *limit* bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


class _Runtime:
    """Settings, service and telemetry, created on first use unless injected (tests)."""

    def __init__(
        self,
        settings: ValidatorSettings | None,
        llm: LLMClient | None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._service: ValidatorService | None = None
        self._telemetry = telemetry
        self._owns_telemetry = False
        self._lock = threading.Lock()

    def telemetry(self) -> Telemetry:
        """The injected facade, or one built from these settings (disabled without keys)."""
        with self._lock:
            if self._telemetry is None:
                self._telemetry = Telemetry(self.settings(), "validator")
                self._owns_telemetry = True
            return self._telemetry

    def shutdown(self) -> None:
        if self._owns_telemetry and self._telemetry is not None:
            self._telemetry.shutdown()

    def settings(self) -> ValidatorSettings:
        if self._settings is None:
            self._settings = load_validator_settings()
        return self._settings

    def service(self) -> ValidatorService:
        if self._service is None:
            settings = self.settings()
            llm = self._llm if self._llm is not None else build_llm(settings)
            self._service = ValidatorService(
                llm=llm,
                model=settings.validate_model,
                timeout_seconds=settings.validate_timeout_seconds,
            )
        return self._service

    def llm_ready(self) -> bool:
        return self._llm is not None or self.settings().llm_enabled


def _trace_input(request: ValidationRequest) -> dict[str, Any]:
    """The model's view of the request, with account IDs replaced by display labels."""
    view = model_view(request)
    labels = {a.account_id: a.display for a in request.bank_metadata.accounts}
    for leg in view["drafted_legs"]:
        leg["source_account"] = labels.get(leg.pop("source_account_id"), "unlisted account")
        if leg["target_id"] in labels:
            leg["target_id"] = labels[leg["target_id"]]
    view["bank_metadata"]["accounts"] = [a.display for a in request.bank_metadata.accounts]
    for target in view["bank_metadata"]["targets"]:
        if target["target_id"] in labels:
            target["target_id"] = "own account"
    return view


def _trace_output(result: ValidationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "verdict": result.verdict,
        "code": result.code,
        "issues": [i.model_dump(mode="json") for i in result.issues or []],
    }


def _outcome(result: ValidationResult) -> tuple[str, str | None]:
    """Telemetry status for a result. Expected outcomes are not crashes."""
    if result.status == "ERROR":
        code = result.code or "VALIDATION_ERROR"
        return ("timeout" if "TIMEOUT" in code else "error"), code
    if result.verdict == "MATCH":
        return "ok", None
    if result.verdict == "UNCERTAIN":
        return "clarification", "VALIDATOR_UNCERTAIN"
    return "rejected", "VALIDATOR_MISMATCH"


def _run_model(
    runtime: _Runtime, telemetry: Telemetry, request: ValidationRequest
) -> ValidationResult:
    """One validation, recorded as a ``validator-llm-call`` generation when a model
    is configured (no generation is recorded for a call that cannot happen)."""
    service = runtime.service()
    if not runtime.llm_ready():
        return service.run(request)
    settings = runtime.settings()
    with telemetry.generation(
        "validator-llm-call",
        model=settings.validate_model,
        prompt_version=PROMPT_VERSION,
        prompt_hash=PROMPT_HASH,
        input=_trace_input(request),
        provider=settings.llm_provider,
        model_parameters={
            "temperature": 0,
            "max_tokens": settings.validate_max_tokens,
            "response_format": {"type": "json_object"},
        },
        attributes={"request_id": request.request_id, "leg_count": len(request.bundle.legs)},
    ) as generation:
        result = service.run(request)
        status, reason = _outcome(result)
        generation.update(
            output=_trace_output(result),
            usage=result.usage,
            latency_ms=result.latency_ms,
            status=status,
            reason_code=reason,
            attributes={"validator_verdict": result.verdict or "ERROR"},
        )
    return result


def _validate_traced(runtime: _Runtime, body: bytes, parent: TraceParent | None) -> tuple[int, Any]:
    """Parse, check and run one validation inside a ``semantic_validation`` span.

    Runs on one worker thread so the span and generation share a trace context.
    Returns ``(http_status, payload)``; for refusals the payload is the error code.
    """
    try:
        parsed: ValidationRequest | None = ValidationRequest.model_validate_json(body)
    except (ValidationError, ValueError, TypeError):  # Money raises TypeError for floats
        parsed = None
    telemetry = runtime.telemetry()
    attributes: dict[str, Any] = {"route": VALIDATE_PATH, "prompt_hash": PROMPT_HASH}
    if parsed is not None:
        attributes.update(
            request_id=parsed.request_id,
            conversation_id=parsed.conversation.conversation_id,
            conversation_revision=parsed.conversation.revision,
            bundle_id=str(parsed.bundle.bundle_id),
            leg_count=len(parsed.bundle.legs),
        )
    with telemetry.workflow_span(
        "semantic_validation",
        session_id=parsed.conversation.conversation_id if parsed else None,
        user_pseudonym=telemetry.pseudonymize(parsed.bundle.owner_user_id) if parsed else None,
        attributes=attributes,
        parent=parent,
    ) as span:
        if parsed is None:
            span.set_status("rejected", reason_code="INVALID_REQUEST")
            return 422, "INVALID_REQUEST"
        problem = request_problem(parsed)
        if problem is not None:
            span.set_status("rejected", reason_code=problem)
            return (413 if problem == "CONTEXT_LIMIT_RESTATE" else 422), problem
        result = _run_model(runtime, telemetry, parsed)
        span.set_attributes({"validator_verdict": result.verdict or "ERROR"})
        status, reason = _outcome(result)
        span.set_status(status, reason_code=reason)
        return 200, result.model_dump(mode="json")


def create_app(
    *,
    settings: ValidatorSettings | None = None,
    llm: LLMClient | None = None,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    """Build the app. Production uses ``app`` below; tests inject settings, a fake LLM
    and (optionally) an in-memory or disabled telemetry facade."""
    runtime = _Runtime(settings, llm, telemetry)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        runtime.shutdown()  # flush telemetry this app created

    application = FastAPI(
        title="DCTA Validator", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Health check: reports configuration status without exposing secrets."""
        s = runtime.settings()
        token_ok = s.service_token_configured
        llm_ok = runtime.llm_ready()
        return {
            "service": "validator",
            "status": "ok",
            "llm": "configured" if llm_ok else "missing-credentials",
            "langfuse": "configured" if s.langfuse_enabled else "missing-credentials",
            "service_token": "configured" if token_ok else "missing",
            "validation": "ready" if token_ok and llm_ok else "refusing",
            "model": s.validate_model,
            "prompt_hash": PROMPT_HASH,
        }

    @application.post(VALIDATE_PATH)
    async def validate(request: Request) -> JSONResponse:
        refusal = _token_refusal(runtime.settings(), request.headers.get("authorization"))
        if refusal is not None:
            return refusal
        body = await _read_capped(request, MAX_REQUEST_BYTES)
        if body is None:
            return _refusal(413, "CONTEXT_LIMIT_RESTATE")
        parent = extract_trace_context(request.headers)  # correlation only, never auth
        status_code, payload = await run_in_threadpool(_validate_traced, runtime, body, parent)
        if status_code != 200:
            return _refusal(status_code, payload)
        return JSONResponse(payload, headers=_NO_STORE)

    return application


app = create_app()
