"""Demo operator console for the SIMULATED out-of-band step-up channel (T-07).

These routes stand in for "the bank sends a code to your phone".  They are
protected by their own credential, ``OPERATOR_TOKEN`` (header
``X-Operator-Token``, at least 32 characters).  When it is not configured the
console is disabled (503).  A request that carries a user session cookie or an
``Authorization`` header (the agent's delegation token) is refused even with
the right token, so neither the browser session nor the agent can reach the
code.  Every response says it is a simulation; this is not production MFA.

- ``GET /operator/step-up`` — usable challenges (no codes).
- ``POST /operator/step-up/{authorization_id}/deliver`` — "deliver" the code
  (repeatable: re-delivery returns the same code and never extends expiry).

Delivery is audited without the code; responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from gateway.drafts import get_draft_store
from gateway.identity import SESSION_COOKIE_NAME
from gateway.stepup import SIMULATION_LABEL, StepUpError, StepUpService, operator_token_status
from gateway.stepup_routes import step_up_error

router = APIRouter(prefix="/operator", tags=["operator (simulated channel)"])

OPERATOR_HEADER = "X-Operator-Token"


def require_operator(request: Request) -> None:
    """Allow only the operator credential; never a user session or agent token."""
    configured = operator_token_status()
    if configured is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OPERATOR_CHANNEL_DISABLED")
    if request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "OPERATOR_CREDENTIAL_REQUIRED")
    supplied = request.headers.get(OPERATOR_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied.encode(), configured.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OPERATOR_CREDENTIAL_REQUIRED")


Operator = Annotated[None, Depends(require_operator)]


def _service() -> StepUpService:
    return StepUpService(get_draft_store().db_path)


@router.get("/step-up")
def list_step_ups(_: Operator, response: Response) -> dict[str, Any]:
    """Challenges awaiting delivery/entry (authorization, user, expiry); no codes."""
    response.headers["Cache-Control"] = "no-store"
    return {"simulation": SIMULATION_LABEL, "challenges": _service().operator_pending()}


@router.post("/step-up/{authorization_id}/deliver", response_model=None)
def deliver_code(authorization_id: str, _: Operator) -> dict[str, Any] | JSONResponse:
    """Simulate delivering the authorization's step-up code to the user's phone."""
    try:
        body = _service().operator_deliver(authorization_id)
    except StepUpError as exc:
        return step_up_error(exc)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
