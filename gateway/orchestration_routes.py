"""``POST /api/conversations/{conversation_id}/draft`` — the browser's "draft this" button.

Session owner only, Origin (CSRF) check, empty body. The gateway runs the agent and then
the independent validator (``gateway/orchestration.py``) and answers with one of:

- 200 ``{"status": "clarify", "question": {"turn_id", "text"}, "conversation_revision"}``
- 200 ``{"status": "ready" | "needs_correction", "bundle_id", "bundle_hash",
  "conversation_revision", "legs": [...], "total_debit", "validation": {...}}``
  (``ready`` only for a bound MATCH whose ``next_action`` is ``CONFIRM``)
- 404 / 502 / 503 / 504 ``{"status": "error", "code": <stable code>}``

The handler is a plain ``def`` (blocking agent/validator calls and SQLite run in the
threadpool). Unexpected failures are logged server-side and answered with
``DRAFT_FAILED``; no stack trace reaches the client. Nothing here can set a verdict.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.identity_routes import _check_origin
from gateway.orchestration import OrchestrationError, draft_conversation

router = APIRouter(tags=["orchestration"])
logger = logging.getLogger("dcta.gateway.orchestration")

_NO_STORE = {"Cache-Control": "no-store"}


class DraftBody(BaseModel):
    """No fields: the client cannot choose the agent, scopes, model or verdict."""

    model_config = ConfigDict(extra="forbid")


def _error(code: str, http_status: int, retry_after: int | None = None) -> JSONResponse:
    headers = dict(_NO_STORE)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=http_status, content={"status": "error", "code": code}, headers=headers
    )


@router.post("/api/conversations/{conversation_id}/draft", response_model=None)
def draft(
    conversation_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
    request: Request,
    body: DraftBody | None = None,
) -> JSONResponse:
    """Draft from the conversation's current revision: a question or a validated draft."""
    _check_origin(request)
    try:
        result = draft_conversation(user.user_id, conversation_id)
    except OrchestrationError as exc:
        return _error(exc.code, exc.http_status, exc.retry_after)
    except Exception:  # noqa: BLE001 - fail closed without leaking internals
        logger.exception("draft orchestration failed")
        return _error("DRAFT_FAILED", 502)
    return JSONResponse(result, headers=_NO_STORE)
