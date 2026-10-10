"""Pydantic request/response models for the conversation and audit API.

These models live in gateway/ (not shared/) because they are HTTP boundary
schemas specific to the gateway conversation and audit routes.  shared/
remains pure types only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _ConvBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Request models ──────────────────────────────────────────────────


class CreateConversationRequest(_ConvBase):
    pass  # no fields; the server assigns everything


class AppendTextRequest(_ConvBase):
    text: str = Field(..., min_length=1, max_length=2000)


class TranscriptEditRequest(_ConvBase):
    edits_turn_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)


class AnswerRequest(_ConvBase):
    answers_question_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)


class ProposeQuestionRequest(_ConvBase):
    text: str = Field(..., min_length=1, max_length=2000)


class AgentEventRequest(_ConvBase):
    step: str = Field(..., min_length=1, max_length=200)
    payload: dict = Field(default_factory=dict)
    trace_id: str | None = Field(default=None, max_length=128)
    span_id: str | None = Field(default=None, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)


# ── Response models ─────────────────────────────────────────────────


class TurnResponse(_ConvBase):
    turn_id: str
    seq: int
    role: str
    text: str
    edits_turn_id: str | None = None
    answers_question_id: str | None = None
    created_at: str
    conversation_revision: int | None = None
    conversation_hash: str | None = None


class ConversationResponse(_ConvBase):
    conversation_id: str
    owner_user_id: str
    revision: int
    conversation_hash: str
    created_at: str
    updated_at: str
    turns: list[TurnResponse]


class SnapshotResponse(_ConvBase):
    conversation_id: str
    revision: int
    conversation_hash: str
    turns: list[TurnResponse]


class AudioUploadResponse(_ConvBase):
    audio_id: str
    conversation_id: str
    sha256_digest: str
    content_type: str
    size_bytes: int
    duration_ms: int | None


class AuditEventResponse(_ConvBase):
    seq: int
    ts: str
    source: str
    step: str
    payload: dict
    trace_id: str | None
    span_id: str | None


class AuditChainResult(_ConvBase):
    ok: bool
    first_bad_seq: int | None
