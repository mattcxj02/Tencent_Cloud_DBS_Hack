"""Semantic validation routes (T-10). Browser session only; owner only.

- ``POST /api/drafts/{bundle_id}/validate``: the gateway builds the request from its
  own records and calls the internal validator. The body must be empty or ``{}``;
  any field, such as a client-supplied ``verdict``, is rejected with 422. Requires the
  session cookie and the WebAuthn Origin (CSRF check), so an agent delegation token
  cannot call it.
- ``GET /api/drafts/{bundle_id}/validation``: the latest attempt for the draft.

200 responses carry the stored result view: ``state`` (CURRENT/SUPERSEDED/PENDING),
``status`` (OK/ERROR), ``verdict``, ``code``, field ``issues``,
``eligible_for_confirmation``, ``next_action`` (CONFIRM, CORRECT_DRAFT, CLARIFY, RETRY,
RESTATE, REDRAFT, WAIT, NONE), attempt counters, hashes, model, prompt hash and trace ID.
Issue ``reason`` text is model output: render it escaped, never as HTML.

Refusals are ``{"detail": <stable code>, ...}``: 404 ``DRAFT_NOT_FOUND`` (unknown or
another user's draft), ``VALIDATION_NOT_FOUND``; 409 ``DRAFT_NOT_ELIGIBLE`` (+
``draft_status``), ``DRAFT_STALE_REVISION``, ``VALIDATION_FINAL`` (+ ``result``),
``VALIDATION_IN_PROGRESS``, ``CONVERSATION_INTEGRITY``, ``BUNDLE_INTEGRITY``,
``BANK_METADATA_UNAVAILABLE``, ``VALIDATION_REQUEST_INVALID``; 429
``VALIDATION_ATTEMPTS_EXHAUSTED``, ``VALIDATION_RATE_LIMITED`` (+ ``Retry-After``).

Handlers are plain ``def``: SQLite and the blocking validator call run in FastAPI's
threadpool. No route, header or parameter can set or skip a verdict.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.identity_routes import _check_origin
from gateway.validation import ValidationRefused, latest_result, validate_draft

router = APIRouter(tags=["validation"])

_NO_STORE = {"Cache-Control": "no-store"}


class ValidateBody(BaseModel):
    """No fields: the client cannot supply a verdict, prompt, model or request data."""

    model_config = ConfigDict(extra="forbid")


def _refusal(exc: ValidationRefused) -> JSONResponse:
    headers = dict(_NO_STORE)
    retry_after = exc.extra.get("retry_after_seconds")
    if exc.http_status == 429 and retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.http_status, content={"detail": exc.code, **exc.extra}, headers=headers
    )


@router.post("/api/drafts/{bundle_id}/validate", response_model=None)
def validate(
    bundle_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
    request: Request,
    body: ValidateBody | None = None,
) -> JSONResponse:
    """Run the independent semantic validation of the user's frozen draft."""
    _check_origin(request)
    try:
        view = validate_draft(user.user_id, bundle_id)
    except ValidationRefused as exc:
        return _refusal(exc)
    return JSONResponse(view, headers=_NO_STORE)


@router.get("/api/drafts/{bundle_id}/validation", response_model=None)
def get_validation(
    bundle_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> JSONResponse:
    """The latest validation attempt of the user's draft (owner only)."""
    try:
        view: dict[str, Any] = latest_result(user.user_id, bundle_id)
    except ValidationRefused as exc:
        return _refusal(exc)
    return JSONResponse(view, headers=_NO_STORE)
