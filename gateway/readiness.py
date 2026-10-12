"""Cheap, non-model preflight checks before the gateway delegates drafting."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from gateway.stepup import _usable, load_step_up_settings
from gateway.validation import get_validator_client
from shared.deadline import DeadlineExpired, current_deadline
from shared.settings import load_gateway_settings

logger = logging.getLogger("dcta.readiness")

# Two local service checks, with a total wall-clock cap even on a stalled response.
READINESS_TIMEOUT_SECONDS = 3.0
SERVICE_CHECK_TIMEOUT_SECONDS = 1.0


def configuration_status() -> dict[str, Any]:
    settings = load_gateway_settings()
    step_up = load_step_up_settings()
    missing = []
    if not settings.service_token_configured:
        missing.append("VALIDATOR_SERVICE_TOKEN")
    if _usable(step_up.operator_token) is None:
        missing.append("OPERATOR_TOKEN")
    if _usable(step_up.step_up_secret) is None:
        missing.append("STEP_UP_SECRET")
    return {
        "missing_configuration": missing,
        "step_up": "configured" if "OPERATOR_TOKEN" not in missing else "unavailable",
        "step_up_persistence": "configured" if "STEP_UP_SECRET" not in missing else "ephemeral",
    }


def drafting_problem() -> str | None:
    """Check token presence, authenticated validator connectivity, then agent config.

    These are service-local GETs, never provider calls. A timed-out worker cannot
    start extraction; HTTP I/O also has its own short timeout. No remote body or
    exception text is returned or logged.
    """
    try:
        if not load_gateway_settings().service_token_configured:
            return "SERVICE_TOKEN_UNSET"
    except Exception:  # noqa: BLE001 - malformed environment must fail closed
        return "SERVICE_CONFIGURATION_INVALID"

    parent_budget = current_deadline.get()
    budget = parent_budget.child(READINESS_TIMEOUT_SECONDS) if parent_budget else None
    result: queue.Queue[str | None] = queue.Queue(maxsize=1)

    def check() -> None:
        try:
            problem = get_validator_client().readiness(
                timeout_seconds=min(SERVICE_CHECK_TIMEOUT_SECONDS, budget.remaining())
                if budget
                else SERVICE_CHECK_TIMEOUT_SECONDS
            )
            if problem is None:
                from gateway.orchestration import get_agent_http

                timeout = (
                    min(SERVICE_CHECK_TIMEOUT_SECONDS, budget.remaining())
                    if budget
                    else SERVICE_CHECK_TIMEOUT_SECONDS
                )
                response = get_agent_http().get("/ready", timeout=timeout)
                problem = (
                    None
                    if response.status_code == 200 and response.json() == {"status": "ready"}
                    else "EXTRACTOR_UNAVAILABLE"
                )
                if response.status_code == 503 and response.json() == {
                    "status": "unavailable",
                    "code": "EXTRACTOR_NOT_CONFIGURED",
                }:
                    problem = "EXTRACTOR_NOT_CONFIGURED"
        except Exception:  # noqa: BLE001 - no transport/configuration internals leave preflight
            problem = "DRAFTING_UNAVAILABLE"
        result.put(problem)

    threading.Thread(target=check, daemon=True).start()
    try:
        return result.get(timeout=budget.remaining() if budget else READINESS_TIMEOUT_SECONDS)
    except (queue.Empty, DeadlineExpired):
        return "READINESS_TIMEOUT"


def log_configuration() -> None:
    """Name missing settings, never their values; step-up is independent of drafting."""
    for name in configuration_status()["missing_configuration"]:
        purpose = (
            "drafting unavailable" if name == "VALIDATOR_SERVICE_TOKEN" else "step-up degraded"
        )
        logger.warning("%s missing or unusable; %s; see README.md setup guidance", name, purpose)
