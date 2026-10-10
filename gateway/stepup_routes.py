"""User routes for simulated step-up (T-07) and authorization cancel.

- ``POST /api/authorizations/{id}/step-up`` ``{code, expected_envelope_hash?}``
  — verify the simulated out-of-band code and, in the same transaction,
  run the full policy recheck and commit (``gateway.execution.commit``).
- ``GET /api/authorizations/{id}/step-up`` — challenge status (never the code).
- ``POST /api/authorizations/{id}/step-up/resend`` — ask the simulated
  channel to re-deliver the same code; the expiry is never extended.
- ``POST /api/authorizations/{id}/cancel`` — the owner cancels a pending
  approval (``PENDING_APPROVAL`` / ``AWAITING_STEP_UP`` → ``CANCELLED``).

All routes need the user's session; mutating routes also check Origin.
Handlers are plain ``def`` (synchronous SQLite in the threadpool).  The code
itself is only ever shown by the operator console (``operator_routes.py``).

200 responses carry ``status`` ``COMMITTED`` (+ receipt) or ``BLOCKED`` (+
``reason_codes``, e.g. ``STEP_UP_ATTEMPTS_EXHAUSTED`` or a policy block found
by the recheck).  Errors are ``{"detail": <stable code>, "reason_codes": [...],
"status"?, "attempts_remaining"?}``: 404 ``AUTHORIZATION_NOT_FOUND`` /
``STEP_UP_NOT_ISSUED``, 503 ``COMMIT_FAILED``, 409 otherwise (e.g.
``STEP_UP_CODE_INVALID``, ``AUTHORIZATION_EXPIRED``, ``STEP_UP_NOT_ACTIVE``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.drafts import get_draft_store
from gateway.identity_routes import _check_origin
from gateway.stepup import StepUpError, StepUpService

router = APIRouter(tags=["step-up"])

User = Annotated[UserPrincipal, Depends(require_user_session)]


class StepUpSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=16)
    expected_envelope_hash: str | None = Field(default=None, min_length=1, max_length=128)


_HTTP_STATUS: dict[str, int] = {
    "AUTHORIZATION_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "STEP_UP_NOT_ISSUED": status.HTTP_404_NOT_FOUND,
    "COMMIT_FAILED": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _service() -> StepUpService:
    return StepUpService(get_draft_store().db_path)


def step_up_error(exc: StepUpError) -> JSONResponse:
    content: dict[str, Any] = {"detail": exc.code, "reason_codes": list(exc.reason_codes)}
    if exc.status is not None:
        content["status"] = exc.status
    if exc.attempts_remaining is not None:
        content["attempts_remaining"] = exc.attempts_remaining
    return JSONResponse(
        status_code=_HTTP_STATUS.get(exc.code, status.HTTP_409_CONFLICT),
        content=content,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/authorizations/{authz_id}/step-up", response_model=None)
def submit_step_up(
    authz_id: str, body: StepUpSubmit, user: User, request: Request
) -> dict[str, Any] | JSONResponse:
    """Submit the simulated step-up code; a valid code commits atomically or blocks."""
    _check_origin(request)
    try:
        return _service().submit(
            user_id=user.user_id,
            authz_id=authz_id,
            code=body.code,
            expected_envelope_hash=body.expected_envelope_hash,
        )
    except StepUpError as exc:
        return step_up_error(exc)


@router.get("/api/authorizations/{authz_id}/step-up", response_model=None)
def step_up_status(authz_id: str, user: User) -> dict[str, Any] | JSONResponse:
    """Challenge status, expiry and attempts remaining (never the code)."""
    try:
        return _service().status(user_id=user.user_id, authz_id=authz_id)
    except StepUpError as exc:
        return step_up_error(exc)


@router.post("/api/authorizations/{authz_id}/step-up/resend", response_model=None)
def resend_step_up(authz_id: str, user: User, request: Request) -> dict[str, Any] | JSONResponse:
    """Re-deliver the same code through the simulated channel; expiry unchanged."""
    _check_origin(request)
    try:
        return _service().resend(user_id=user.user_id, authz_id=authz_id)
    except StepUpError as exc:
        return step_up_error(exc)


@router.post("/api/authorizations/{authz_id}/cancel", response_model=None)
def cancel_authorization(
    authz_id: str, user: User, request: Request
) -> dict[str, Any] | JSONResponse:
    """Cancel a pending approval; any step-up challenge is closed with it."""
    _check_origin(request)
    try:
        return _service().cancel(user_id=user.user_id, authz_id=authz_id)
    except StepUpError as exc:
        return step_up_error(exc)
