"""``POST /api/conversations/{conversation_id}/draft`` — the browser's "draft this" button.

Session owner only, Origin (CSRF) check. Optional operation/revision IDs and
validation-only mode; no payment payload. The gateway runs the agent and then
the independent validator (``gateway/orchestration.py``) and answers with one of:

- 200 ``{"status": "clarify", "question": {"turn_id", "text"}, "conversation_revision"}``
- 200 ``{"status": "ready" | "needs_correction", "bundle_id", "bundle_hash",
  "conversation_revision", "legs": [...], "total_debit", "validation": {...}}``
  (``ready`` only for a bound MATCH whose ``next_action`` is ``CONFIRM``)
- 404 / 502 / 503 / 504 ``{"status": "error", "code": <stable code>}``

The async handler observes disconnects while blocking agent/validator calls and SQLite
run in the threadpool. Unexpected failures are logged server-side and answered with
``DRAFT_FAILED``; no stack trace reaches the client. Nothing here can set a verdict.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from gateway.auth.delegation import Principal, require_principal
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.identity_routes import _check_origin
from gateway.orchestration import OrchestrationError, draft_conversation
from shared.deadline import Deadline
from shared.settings import load_gateway_settings

router = APIRouter(tags=["orchestration"])
logger = logging.getLogger("dcta.gateway.orchestration")

_NO_STORE = {"Cache-Control": "no-store"}


class DraftBody(BaseModel):
    """Browser correlation and retry intent; no agent, model, scopes or verdict."""

    model_config = ConfigDict(extra="forbid")
    operation_id: UUID | None = None
    revision: int | None = None
    mode: Literal["auto", "validation"] = "auto"


def _error(code: str, http_status: int, retry_after: int | None = None) -> JSONResponse:
    headers = dict(_NO_STORE)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=http_status, content={"status": "error", "code": code}, headers=headers
    )


@router.post("/api/conversations/{conversation_id}/draft", response_model=None)
async def draft(
    conversation_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
    request: Request,
    body: DraftBody | None = None,
) -> JSONResponse:
    """Draft from the conversation's current revision: a question or a validated draft."""
    _check_origin(request)
    cancelled = threading.Event()
    try:
        budget = Deadline.after(load_gateway_settings().draft_timeout_seconds)
    except Exception:  # noqa: BLE001 - invalid configuration fails before admission
        return _error("SERVICE_CONFIGURATION_INVALID", 503)
    budget.cancelled = cancelled
    task = asyncio.create_task(
        run_in_threadpool(
            draft_conversation,
            user.user_id,
            conversation_id,
            cancelled=cancelled,
            budget=budget,
            operation_id=str(body.operation_id) if body and body.operation_id else None,
            expected_revision=body.revision if body else None,
            validation_only=bool(body and body.mode == "validation"),
        )
    )

    def consume_result(done: asyncio.Task) -> None:
        if not done.cancelled():
            done.exception()

    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if await request.is_disconnected():
                cancelled.set()
            if not budget.active():
                # Allow mandatory finalization, but never wait for threadpool admission
                # or an uncancellable remote request beyond the scheduling allowance.
                done, _ = await asyncio.wait({task}, timeout=0.1)
                if not done:
                    task.add_done_callback(consume_result)
                    code = "DRAFT_CANCELLED" if cancelled.is_set() else "ORCHESTRATION_TIMEOUT"
                    cancelled.set()
                    return _error(code, 503 if code == "DRAFT_CANCELLED" else 504)
        result = await task
    except asyncio.CancelledError:
        cancelled.set()
        task.add_done_callback(consume_result)
        raise
    except OrchestrationError as exc:
        return _error(exc.code, exc.http_status, exc.retry_after)
    except Exception:  # noqa: BLE001 - fail closed without leaking internals
        logger.exception("draft orchestration failed")
        return _error("DRAFT_FAILED", 502)
    return JSONResponse(result, headers=_NO_STORE)


@router.get("/api/conversations/{conversation_id}/progress")
def progress(
    conversation_id: str,
    operation_id: UUID,
    revision: int,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> JSONResponse:
    from gateway.orchestration import _bundle_response, _db_path, _owned_conversation
    from gateway.progress import get
    from gateway.validation import latest_result
    from ledger.schema import connect

    db_path = _db_path()
    conn = connect(db_path)
    try:
        current = _owned_conversation(conn, user.user_id, conversation_id)
    except OrchestrationError as exc:
        return _error(exc.code, exc.http_status)
    finally:
        conn.close()
    record = get(db_path, user.user_id, conversation_id, str(operation_id))
    if record is None:
        return _error("PROGRESS_NOT_FOUND", 404)
    view = record.view()
    result = view["result"]
    published_revision = (
        result["turn"]["conversation_revision"]
        if record.kind == "voice" and result and result.get("turn")
        else record.revision
    )
    if record.revision != revision or current != published_revision:
        return _error("CONVERSATION_CHANGED", 409)
    if result and result.get("status") == "ready":
        try:
            validation = latest_result(user.user_id, result["bundle_id"], db_path=db_path)
            view["result"] = _bundle_response(
                db_path, user.user_id, result["bundle_id"], validation
            )
        except OrchestrationError as exc:
            view.update(stage="failed", code=exc.code, result=None)
    return JSONResponse(view, headers=_NO_STORE)


class CancelDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    revision: int


@router.post("/api/conversations/{conversation_id}/draft/cancel")
def cancel_draft(
    conversation_id: str,
    body: CancelDraftBody,
    request: Request,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> JSONResponse:
    from gateway.drafting_work import cancel_operation
    from gateway.orchestration import _db_path, _owned_conversation
    from gateway.progress import get
    from ledger.schema import connect

    _check_origin(request)
    db_path = _db_path()
    conn = connect(db_path)
    try:
        _owned_conversation(conn, user.user_id, conversation_id)
    except OrchestrationError as exc:
        return _error(exc.code, exc.http_status)
    finally:
        conn.close()
    record = get(db_path, user.user_id, conversation_id, str(body.operation_id))
    if record is None or record.revision != body.revision or record.kind != "draft":
        return _error("PROGRESS_NOT_FOUND", 404)
    stopped = cancel_operation(record)
    return JSONResponse(
        {"status": "draft_stopped" if stopped else "draft_not_running"}, headers=_NO_STORE
    )


class AgentProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    stage: Literal["extracting", "resolving"]


@router.post("/internal/conversations/{conversation_id}/progress")
def agent_progress(
    conversation_id: str,
    body: AgentProgressBody,
    principal: Annotated[Principal, Depends(require_principal("conversation:read"))],
) -> JSONResponse:
    from gateway.drafting_work import report_stage
    from shared.deadline import DeadlineExpired

    try:
        report_stage(
            principal.run_id, principal.user_id, conversation_id, body.revision, body.stage
        )
    except DeadlineExpired:
        return _error("DRAFT_WORK_INACTIVE", 409)
    return JSONResponse({"status": "recorded"}, headers=_NO_STORE)
