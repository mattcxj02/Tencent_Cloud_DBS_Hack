"""Mandate settings routes: view and signed ``update_mandate`` changes (T-07).

- ``GET /api/mandate`` — current settings, any pending (cooling) change with
  its activation time, recent changes, and the scope note.
- ``POST /api/mandate/changes`` — request a change (any of ``per_leg_limit``,
  ``daily_limit``, ``allowed_actions``, ``paused``) with the passkey
  ``credential_id``.  The gateway validates it deterministically, rejects
  mixed tighten/loosen requests, and issues an ``update_mandate`` signing
  envelope (no conversation binding, TTL 120 s) with WebAuthn options whose
  challenge is exactly ``approval_challenge(envelope)``.
- ``GET /api/mandate/changes/{authorization_id}`` — that envelope again.
- ``POST /api/mandate/changes/{authorization_id}/approve`` — passkey approval;
  tightening applies at once, loosening cools for ``COOLING_HOURS``.

The operation is fixed by the route (never a request field).  An
``update_mandate`` authorization is refused on the payment approve route and
a payment authorization is refused here (``OPERATION_MISMATCH``).  Session
required; mutating routes check Origin; handlers are plain ``def``.

Pausing stops payments through this conversational assistant only; it does
not lock cards, accounts or other banking channels (simulated bank).

Errors: ``{"detail": <stable code>, "reason_codes": [...]}`` — 404
``AUTHORIZATION_NOT_FOUND`` / ``MANDATE_NOT_FOUND``; 422 for change-rule
codes (``MANDATE_CHANGE_MIXED``, ``MANDATE_NO_CHANGE``, ...); 503
``COMMIT_FAILED``; 409 otherwise.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.drafts import get_draft_store
from gateway.identity_routes import _check_origin
from gateway.mandates import REASON_CODES, MandateError, MandateService
from gateway.telemetry import stage
from shared.schemas import Action

router = APIRouter(tags=["mandate"])

User = Annotated[UserPrincipal, Depends(require_user_session)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MandateChangeRequest(_Strict):
    """Requested settings; omitted fields stay unchanged.  Money as decimal strings."""

    credential_id: str = Field(..., min_length=1)
    per_leg_limit: str | None = Field(default=None, min_length=1, max_length=16)
    daily_limit: str | None = Field(default=None, min_length=1, max_length=16)
    allowed_actions: list[Action] | None = Field(default=None, max_length=8)
    paused: StrictBool | None = None


class MandateApproveRequest(_Strict):
    authorization_id: str = Field(..., min_length=1)
    expected_envelope_hash: str = Field(..., min_length=1)
    assertion: dict[str, Any]


_RULE_CODES = frozenset(REASON_CODES) - {
    "MANDATE_NOT_FOUND",
    "MANDATE_VERSION_CHANGED",
    "MANDATE_TIGHTENED",
    "MANDATE_ACTIVATED",
    "MANDATE_CHANGE_REPLACED",
    "MANDATE_CHANGE_CANCELLED_BY_TIGHTENING",
}

_HTTP_STATUS: dict[str, int] = {
    "AUTHORIZATION_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "MANDATE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "COMMIT_FAILED": status.HTTP_503_SERVICE_UNAVAILABLE,
}


#: Telemetry attributes of signed mandate-change spans (T-08); no limits or amounts.
_MANDATE_OP = {"operation_type": "update_mandate"}


def _service() -> MandateService:
    return MandateService(get_draft_store().db_path)


def _error(exc: MandateError) -> JSONResponse:
    # 422: the request breaks a deterministic change rule.
    http = 422 if exc.code in _RULE_CODES else _HTTP_STATUS.get(exc.code, 409)
    content: dict[str, Any] = {"detail": exc.code, "reason_codes": list(exc.reason_codes)}
    if exc.code in REASON_CODES:
        content["message"] = REASON_CODES[exc.code]
    return JSONResponse(status_code=http, content=content)


@router.get("/api/mandate", response_model=None)
def get_mandate(user: User) -> dict[str, Any] | JSONResponse:
    """Current mandate, pending change and activation time, scope note."""
    try:
        return _service().view(user.user_id)
    except MandateError as exc:
        return _error(exc)


@router.post("/api/mandate/changes", response_model=None)
def request_mandate_change(
    body: MandateChangeRequest, user: User, request: Request
) -> dict[str, Any] | JSONResponse:
    """Validate the change and issue its ``update_mandate`` signing envelope."""
    _check_origin(request)
    service = _service()
    with stage("challenge", user_id=user.user_id, attributes=_MANDATE_OP) as span:
        try:
            authz_id = service.request_change(
                user_id=user.user_id,
                credential_id=body.credential_id,
                per_leg_limit=body.per_leg_limit,
                daily_limit=body.daily_limit,
                allowed_actions=[a.value for a in body.allowed_actions]
                if body.allowed_actions is not None
                else None,
                paused=body.paused,
            )
            view = service.change_view(user.user_id, authz_id)
        except MandateError as exc:
            span.set_status("rejected", reason_code=exc.code)
            return _error(exc)
        span.set_attributes({"authorization_id": authz_id})
        return view


@router.get("/api/mandate/changes/{authz_id}", response_model=None)
def get_mandate_change(authz_id: str, user: User) -> dict[str, Any] | JSONResponse:
    """The stored ``update_mandate`` envelope with WebAuthn options (owner only)."""
    try:
        return _service().change_view(user.user_id, authz_id)
    except MandateError as exc:
        return _error(exc)


@router.post("/api/mandate/changes/{authz_id}/approve", response_model=None)
def approve_mandate_change(
    authz_id: str, body: MandateApproveRequest, user: User, request: Request
) -> dict[str, Any] | JSONResponse:
    """Approve with a passkey assertion over the stored envelope and apply the change."""
    _check_origin(request)
    if body.authorization_id != authz_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "AUTHORIZATION_ID_MISMATCH")
    with stage(
        "approval", user_id=user.user_id, attributes={**_MANDATE_OP, "authorization_id": authz_id}
    ) as span:
        try:
            result = _service().approve_change(
                user_id=user.user_id,
                authz_id=authz_id,
                expected_envelope_hash=body.expected_envelope_hash,
                assertion=body.assertion,
            )
        except MandateError as exc:
            status_ = "expired" if "EXPIRED" in exc.code else "rejected"
            span.set_status(
                "error" if exc.code == "COMMIT_FAILED" else status_, reason_code=exc.code
            )
            return _error(exc)
        if isinstance(result, dict):
            span.set_attributes({"approval_state": result.get("status")})
        return result
