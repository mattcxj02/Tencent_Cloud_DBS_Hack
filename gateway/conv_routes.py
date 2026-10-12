"""Conversation, audio and audit routes for the gateway.

User routes (via ``require_user_session``, owner-checked):
    POST   /api/conversations                        create conversation
    POST   /api/conversations/{id}/text              append user_text turn
    POST   /api/conversations/{id}/transcript-edit   append user_transcript_edit
    POST   /api/conversations/{id}/answer            append user_answer
    POST   /api/conversations/{id}/audio             upload audio evidence
    GET    /api/conversations/{id}                   get own conversation
    GET    /api/conversations/{id}/audit             get own audit events
    GET    /api/conversations/{id}/audit/verify      verify chain

Internal routes (delegation token):
    GET    /internal/conversations/{id}/snapshot      requires conversation:read
    POST   /internal/conversations/{id}/questions     requires conversation:propose_question
    POST   /internal/audit/agent-events               requires audit:append_agent

There is NO internal route to append user turns.  Agent credentials
cannot create ``user_*`` roles by any path.

There is no route that accepts a client-supplied ASR transcript:
``user_asr_final`` turns are produced only by the gateway's own TRTC call in
``POST /api/conversations/{id}/voice`` (``gateway/asr_routes.py``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel

from gateway.audit import (
    ALLOWED_AGENT_STEPS,
    FORBIDDEN_STEP_PREFIXES,
    append_audit,
    verify_chain,
)
from gateway.auth.delegation import Principal, require_principal
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank_routes import get_bank_store
from gateway.conv_models import (
    AgentEventRequest,
    AnswerRequest,
    AppendTextRequest,
    AudioUploadResponse,
    AuditChainResult,
    AuditEventResponse,
    ConversationResponse,
    CreateConversationRequest,
    ProposeQuestionRequest,
    SnapshotResponse,
    TranscriptEditRequest,
    TurnResponse,
)
from gateway.conversations import (
    MAX_AUDIO_SIZE_BYTES,
    ContextLimitError,
    ConversationStore,
)
from gateway.identity_routes import _check_origin
from shared.canonical import canonical_bytes
from shared.deadline import DeadlineExpired

router = APIRouter(tags=["conversation"])

# Module-level store; initialized lazily.
_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store  # noqa: PLW0603
    if _store is None:
        from shared.settings import load_gateway_settings

        settings = load_gateway_settings()
        _store = ConversationStore(settings.db_path)
    return _store


def _set_conversation_store(store: ConversationStore | None) -> None:
    """Test override: inject a store with a temp DB path."""
    global _store  # noqa: PLW0603
    _store = store


def _context_limit_exc() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={
            "code": ContextLimitError.CODE,
            "message": "context limit exceeded; restate explicitly",
        },
    )


# ── Dependencies ────────────────────────────────────────────────────

UserSession = Annotated[UserPrincipal, Depends(require_user_session)]

# Pre-built dependencies for internal scopes.
ConvReadPrincipal = Annotated[Principal, Depends(require_principal("conversation:read"))]
QuestionPrincipal = Annotated[
    Principal, Depends(require_principal("conversation:propose_question"))
]
AuditPrincipal = Annotated[Principal, Depends(require_principal("audit:append_agent"))]


# ── User routes ─────────────────────────────────────────────────────


@router.post("/api/conversations", response_model=ConversationResponse)
async def create_conversation(
    _body: CreateConversationRequest,
    user: UserSession,
) -> dict:
    store = get_conversation_store()
    return store.create_conversation(user.user_id)


@router.post(
    "/api/conversations/{conversation_id}/text",
    response_model=TurnResponse,
)
async def append_text_turn(
    conversation_id: str,
    body: AppendTextRequest,
    user: UserSession,
) -> dict:
    store = get_conversation_store()
    try:
        result = store.append_text_turn(user.user_id, conversation_id, body.text)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ContextLimitError:
        raise _context_limit_exc() from None
    return result


@router.post(
    "/api/conversations/{conversation_id}/transcript-edit",
    response_model=TurnResponse,
)
async def append_transcript_edit(
    conversation_id: str,
    body: TranscriptEditRequest,
    user: UserSession,
) -> dict:
    store = get_conversation_store()
    try:
        result = store.append_transcript_edit(
            user.user_id, conversation_id, body.edits_turn_id, body.text
        )
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ContextLimitError:
        raise _context_limit_exc() from None
    return result


@router.post(
    "/api/conversations/{conversation_id}/answer",
    response_model=TurnResponse,
)
async def append_answer(
    conversation_id: str,
    body: AnswerRequest,
    user: UserSession,
    request: Request,
) -> dict:
    if body.selections:
        _check_origin(request)
    store = get_conversation_store()
    try:
        result = store.append_answer(
            user.user_id,
            conversation_id,
            body.answers_question_id,
            body.text,
            question_revision=body.question_revision,
            selections=body.selections,
        )
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from None
    except ValueError as exc:
        code = status.HTTP_400_BAD_REQUEST if body.selections else status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=code, detail=str(exc)) from None
    except ContextLimitError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": ContextLimitError.CODE,
                "message": "context limit exceeded; restate explicitly",
            },
        ) from None
    return result


@router.post(
    "/api/conversations/{conversation_id}/audio",
    response_model=AudioUploadResponse,
)
async def upload_audio(
    conversation_id: str,
    user: UserSession,
    file: UploadFile = File(...),
    consent: bool = False,
) -> dict:
    store = get_conversation_store()
    content_type = file.content_type or ""
    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="audio file too large",
        )
    try:
        result = store.upload_audio(
            user.user_id,
            conversation_id,
            audio_bytes,
            content_type,
            consent=consent,
        )
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return result


@router.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    user: UserSession,
) -> dict:
    store = get_conversation_store()
    conv = store.get_conversation(user.user_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return conv


@router.get(
    "/api/conversations/{conversation_id}/audit",
    response_model=list[AuditEventResponse],
)
async def get_audit_events(
    conversation_id: str,
    user: UserSession,
) -> list[dict]:
    """Return audit events for the user's conversation."""
    store = get_conversation_store()
    conv = store.get_conversation(user.user_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    from ledger.schema import connect

    bank_store = get_bank_store()
    conn = connect(bank_store._db_path)
    try:
        rows = conn.execute(
            """
            SELECT seq, ts, source, step, payload_json, trace_id, span_id
            FROM audit_events
            WHERE conversation_id = ?
            ORDER BY seq
            """,
            (conversation_id,),
        ).fetchall()
        import json

        return [
            {
                "seq": int(r["seq"]),
                "ts": r["ts"],
                "source": r["source"],
                "step": r["step"],
                "payload": json.loads(r["payload_json"]),
                "trace_id": r["trace_id"],
                "span_id": r["span_id"],
            }
            for r in rows
        ]
    finally:
        conn.close()


@router.get(
    "/api/conversations/{conversation_id}/audit/verify",
    response_model=AuditChainResult,
)
async def verify_audit_chain(
    conversation_id: str,
    user: UserSession,
) -> dict:
    """Verify the hash chain for the user's conversation audit events."""
    store = get_conversation_store()
    conv = store.get_conversation(user.user_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    from ledger.schema import connect

    bank_store = get_bank_store()
    conn = connect(bank_store._db_path)
    try:
        return verify_chain(conn, conversation_id=conversation_id)
    finally:
        conn.close()


# ── Internal routes (delegation token) ──────────────────────────────


@router.get(
    "/internal/conversations/{conversation_id}/snapshot",
    response_model=SnapshotResponse,
)
async def get_snapshot(
    conversation_id: str,
    principal: ConvReadPrincipal,
) -> dict:
    """Return an ordered snapshot for agent use.

    Requires ``conversation:read`` scope, ``principal.conversation_id ==
    conversation_id``, and the principal's user_id must own the conversation.
    """
    if principal.conversation_id != conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not bound to this conversation",
        )

    store = get_conversation_store()
    snapshot = store.get_snapshot(conversation_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Verify ownership: the delegation token's user_id must own the conversation.
    from ledger.schema import connect

    bank_store = get_bank_store()
    conn = connect(bank_store._db_path)
    try:
        owner = store.conversation_owner(conn, conversation_id)
        if owner is None or owner != principal.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="not found",
            )
    finally:
        conn.close()

    return snapshot


@router.post(
    "/internal/conversations/{conversation_id}/questions",
    response_model=TurnResponse,
)
async def propose_question(
    conversation_id: str,
    body: ProposeQuestionRequest,
    principal: QuestionPrincipal,
) -> dict:
    """Record an ``app_question`` turn (does not bump user revision).

    Requires ``conversation:propose_question`` scope and
    ``principal.conversation_id == conversation_id``.
    """
    if principal.conversation_id != conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not bound to this conversation",
        )

    store = get_conversation_store()
    snapshot = store.get_snapshot(conversation_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    from ledger.schema import connect

    bank_store = get_bank_store()
    conn = connect(bank_store._db_path)
    try:
        owner = store.conversation_owner(conn, conversation_id)
        if owner is None or owner != principal.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="not found",
            )
    finally:
        conn.close()

    try:
        return store.append_question(
            conversation_id,
            body.text,
            revision=body.revision,
            fields=body.fields,
            extraction=body.extraction,
            run_id=principal.run_id,
        )
    except DeadlineExpired:
        raise HTTPException(status_code=409, detail="DRAFT_WORK_INACTIVE") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ContextLimitError:
        raise _context_limit_exc() from None


# ── Agent audit events ──────────────────────────────────────────────


MAX_AGENT_EVENT_PAYLOAD_BYTES = 8192


class AgentEventResponse(BaseModel):
    seq: int


@router.post(
    "/internal/audit/agent-events",
    response_model=AgentEventResponse,
)
async def append_agent_event(
    body: AgentEventRequest,
    principal: AuditPrincipal,
) -> dict:
    """Append an agent-reported audit event.

    The ``step`` must be in the allowlist (``agent.*``) and must not
    start with a gateway-trusted prefix (``gateway.``, ``approval.``,
    ``execution.``).  Stored with ``source="agent"``.
    """
    step = body.step

    # Reject gateway-trusted step prefixes.
    for prefix in FORBIDDEN_STEP_PREFIXES:
        if step.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"agent events cannot use gateway-trusted step: {step}",
            )

    # Must be in the allowlist.
    if step not in ALLOWED_AGENT_STEPS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"step not in allowlist: {step}",
        )

    # The conversation comes from the delegation token, never from the body:
    # an agent can only report events for the conversation it was handed.
    if principal.conversation_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent events require a conversation-bound token",
        )
    if body.conversation_id is not None and body.conversation_id != principal.conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="conversation_id does not match the delegation token",
        )
    if len(canonical_bytes(body.payload)) > MAX_AGENT_EVENT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="agent event payload too large",
        )

    bank_store = get_bank_store()
    from ledger.schema import connect

    conn = connect(bank_store._db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = get_conversation_store().conversation_owner(conn, principal.conversation_id)
        if owner is None or owner != principal.user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        seq = append_audit(
            conn,
            source="agent",
            conversation_id=principal.conversation_id,
            user_id=principal.user_id,
            step=step,
            payload=body.payload,
            trace_id=body.trace_id,
            span_id=body.span_id,
        )
        conn.commit()
        return {"seq": seq}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
