"""Validation run: one model call under a deadline, then strict output checks.

Every failure path returns ``status == "ERROR"`` with a stable code. Nothing is coerced
into a verdict: a timeout, provider error, empty or truncated reply, tool call, non-JSON
text, duplicate key, extra key, unknown enum value, MATCH with issues, MISMATCH without
issues, unknown leg ID or out-of-range evidence span all produce ERROR.

A schema-valid verdict is still a fallible model judgement. The gateway must bind it to
the bundle and conversation it checked, and a MATCH only permits the confirmation screen.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import ValidationError

from shared.clients.validator import (
    ErrorCode,
    TokenUsage,
    ValidationRequest,
    ValidationResult,
    VerdictPayload,
    validation_input_hash,
    verdict_problem,
)
from shared.deadline import Deadline, DeadlineExpired, bounded_call
from validator.llm import LLMClient, LLMConfigurationError, LLMReply, LLMTimeoutError
from validator.prompt import PROMPT_HASH, build_messages

__all__ = ["MAX_OUTPUT_CHARS", "ValidatorService", "interpret_reply"]

logger = logging.getLogger("dcta.validator")

#: Model replies longer than this are rejected rather than parsed.
MAX_OUTPUT_CHARS = 16_000

# Calls run on worker threads so a hung provider call cannot outlive the deadline for
# the caller. The SDK timeout ends the worker's call eventually as well.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="validator-llm")


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def interpret_reply(reply: LLMReply, request: ValidationRequest) -> VerdictPayload | ErrorCode:
    """Return the verified verdict payload, or the error code that rejects the reply."""
    if reply.has_tool_calls:
        return "LLM_UNEXPECTED_TOOL_CALL"
    content = (reply.content or "").strip()
    # Checked first: a reasoning model that spends the whole budget returns empty content
    # with finish_reason "length", which is truncation, not a silent empty answer.
    if reply.finish_reason == "length":
        return "LLM_OUTPUT_TRUNCATED"
    if not content:
        return "LLM_EMPTY_OUTPUT"
    if reply.finish_reason != "stop":
        return "LLM_ABNORMAL_FINISH"
    if len(content) > MAX_OUTPUT_CHARS:
        return "OUTPUT_SCHEMA_INVALID"
    try:
        data = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValueError:  # JSONDecodeError, duplicate keys, NaN/Infinity
        return "OUTPUT_NOT_JSON"
    if not isinstance(data, dict):
        return "OUTPUT_SCHEMA_INVALID"
    try:
        payload = VerdictPayload.model_validate(data, strict=True)
    except ValidationError:
        return "OUTPUT_SCHEMA_INVALID"
    problem = verdict_problem(request, payload.verdict, payload.issues)
    return problem if problem is not None else payload


class ValidatorService:
    """Runs one validation. ``llm`` is None when the provider key is missing."""

    def __init__(
        self,
        *,
        llm: LLMClient | None,
        model: str,
        timeout_seconds: float,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._timeout = timeout_seconds
        self._executor = executor or _EXECUTOR

    def run(
        self, request: ValidationRequest, *, deadline: Deadline | None = None
    ) -> ValidationResult:
        input_hash = validation_input_hash(request)
        started = time.monotonic()
        budget = (
            deadline.child(self._timeout) if deadline is not None else Deadline.after(self._timeout)
        )
        outcome, usage = self._call_and_check(request, budget)
        latency_ms = int((time.monotonic() - started) * 1000)
        common: dict[str, Any] = {
            "model": self._model,
            "prompt_hash": PROMPT_HASH,
            "input_hash": input_hash,
            "latency_ms": latency_ms,
            "usage": usage,
        }
        if isinstance(outcome, VerdictPayload):
            result = ValidationResult(
                status="OK", verdict=outcome.verdict, issues=list(outcome.issues), **common
            )
        else:
            result = ValidationResult(status="ERROR", code=outcome, **common)
        logger.info(
            "validation request_id=%s status=%s verdict=%s code=%s latency_ms=%d",
            request.request_id,
            result.status,
            result.verdict,
            result.code,
            latency_ms,
        )
        return result

    def _call_and_check(
        self, request: ValidationRequest, budget: Deadline
    ) -> tuple[VerdictPayload | ErrorCode, TokenUsage | None]:
        if self._llm is None:
            return "LLM_NOT_CONFIGURED", None
        messages = build_messages(request)

        def call() -> LLMReply:
            return self._llm.complete(messages, timeout_seconds=budget.remaining())

        try:
            reply = bounded_call(self._executor, budget, call)
        except LLMConfigurationError:
            return "LLM_CONFIGURATION_ERROR", None
        except (DeadlineExpired, LLMTimeoutError):
            return "LLM_TIMEOUT", None
        except Exception as exc:  # noqa: BLE001 - every provider failure fails closed
            logger.warning("validator model call failed: %s", type(exc).__name__)
            return "LLM_PROVIDER_ERROR", None
        return interpret_reply(reply, request), reply.usage
