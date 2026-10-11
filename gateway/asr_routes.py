"""Voice routes (T-12): push-to-talk clip → stored evidence → TRTC ASR → one user turn.

User routes (session owner; other users' conversations and clips return 404):
    POST /api/conversations/{id}/voice              upload one WAV clip and transcribe it
    GET  /api/conversations/{id}/voice              list the conversation's audio evidence
    GET  /api/conversations/{id}/voice/{audio_id}   the stored WAV bytes (digest re-checked)

``POST .../voice`` is a sync handler (the TRTC SDK blocks) and requires the RP Origin.
Multipart fields: ``audio`` (``audio/wav``: RIFF/WAVE, PCM 16-bit, mono, 16 kHz,
<= 60 s, <= 3 MB) and ``consent=true`` (storing the recording needs consent).

Order: Origin → owner → consent → content type and WAV checks → duplicate check →
store the exact bytes (``ConversationStore.upload_audio``: SHA-256, size, duration,
consent, retention) → read the stored file back and check its digest → TRTC with
those bytes → append one ``user_asr_final`` turn linked to the clip
(``audio_evidence.turn_id``) in one transaction. Every step is audited without audio
bytes, transcript text or secrets. An ASR failure keeps the audio and appends no
turn; the text routes keep working. There is no route that accepts a client-supplied
ASR transcript: the gateway produces ``user_asr_final`` itself. Corrections use
``POST /api/conversations/{id}/transcript-edit``.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from gateway.asr import (
    ASR_ENGINE,
    ASR_HTTP_STATUS,
    ASR_LANGUAGE,
    MAX_VOICE_BYTES,
    AsrError,
    AsrResult,
    AudioRejected,
    DuplicateSegment,
    append_voice_turn,
    check_content_type,
    find_linked_duplicate,
    get_transcriber,
    validate_wav,
)
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.conv_models import TurnResponse
from gateway.conv_routes import get_conversation_store
from gateway.conversations import ContextLimitError, ConversationStore
from gateway.identity_routes import _check_origin
from ledger.schema import connect
from shared.telemetry import Telemetry
from shared.timeutil import now_utc, to_iso

router = APIRouter(tags=["voice"])

UserSession = Annotated[UserPrincipal, Depends(require_user_session)]

_ROUTE = "/api/conversations/{conversation_id}/voice"

#: Retention recorded with each consented clip (prototype default; no purge job yet).
VOICE_RETENTION_DAYS = 90


# ── Response models ──────────────────────────────────────────────────


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoiceAsrInfo(_Model):
    request_id: str
    latency_ms: int
    audio_duration_ms: int
    engine: str
    language: str


class VoiceResponse(_Model):
    conversation_id: str
    audio_id: str
    digest: str
    size_bytes: int
    duration_ms: int
    consent: bool
    retention_until: str
    transcript: str
    turn: TurnResponse | None
    asr: VoiceAsrInfo
    trace_id: str | None = None


class AudioClip(_Model):
    audio_id: str
    digest: str
    content_type: str
    size_bytes: int
    duration_ms: int | None
    consent: bool
    retention_until: str
    created_at: str
    turn_id: str | None


class AudioClipList(_Model):
    conversation_id: str
    clips: list[AudioClip]


# ── Telemetry wiring (tests replace it; HTTP cannot) ─────────────────

_telemetry: Telemetry | None = None
_wiring_lock = threading.Lock()


def get_voice_telemetry() -> Telemetry:
    """The gateway's process-wide Langfuse facade (disabled without keys)."""
    with _wiring_lock:
        if _telemetry is not None:
            return _telemetry
    from gateway.telemetry import get_gateway_telemetry

    return get_gateway_telemetry()


def _set_voice_telemetry(telemetry: Telemetry | None) -> None:
    """Test hook: inject an in-memory or disabled telemetry facade."""
    global _telemetry
    with _wiring_lock:
        _telemetry = telemetry


# ── Helpers ──────────────────────────────────────────────────────────


def _fail(http_status: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update({k: v for k, v in extra.items() if v is not None})
    return HTTPException(status_code=http_status, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


def _owns(store: ConversationStore, user_id: str, conversation_id: str) -> bool:
    conn = store._conn()
    try:
        return store.conversation_owner(conn, conversation_id) == user_id
    finally:
        conn.close()


def _audit(
    store: ConversationStore,
    user_id: str,
    conversation_id: str,
    step: str,
    payload: dict[str, Any],
    trace: tuple[str | None, str | None],
) -> None:
    """One audit row in its own transaction (no audio, transcript or secrets)."""
    from gateway.audit import append_audit

    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        append_audit(
            conn,
            source="gateway",
            conversation_id=conversation_id,
            user_id=user_id,
            step=step,
            payload={k: v for k, v in payload.items() if v is not None},
            trace_id=trace[0],
            span_id=trace[1],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_upload(audio: UploadFile) -> bytes:
    data = audio.file.read(MAX_VOICE_BYTES + 1)
    if len(data) > MAX_VOICE_BYTES:
        raise AudioRejected(
            "AUDIO_TOO_LARGE", f"audio exceeds {MAX_VOICE_BYTES} bytes", http_status=413
        )
    return data


def _read_stored(store: ConversationStore, audio_id: str) -> tuple[bytes, str]:
    """Read the stored evidence file and confirm it still matches its recorded digest."""
    conn = store._conn()
    try:
        row = conn.execute(
            "SELECT file_path, sha256_digest FROM audio_evidence WHERE audio_id = ?",
            (audio_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("stored audio row missing")
    data = Path(row["file_path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256_digest"]:
        raise RuntimeError("stored audio digest mismatch")
    return data, row["sha256_digest"]


# ── POST voice ───────────────────────────────────────────────────────


def _user_hotwords(store: ConversationStore, user_id: str) -> list[str]:
    """The user's saved payee names and aliases and saved billers, as ASR hotwords.

    Only the user's own bank records; a failure here must never block transcription.
    """
    try:
        conn = connect(store._db_path)
        try:
            rows = conn.execute(
                "SELECT display_name AS w FROM payees WHERE owner_user_id = ? "
                "UNION SELECT alias FROM payee_aliases WHERE owner_user_id = ? "
                "UNION SELECT b.display_name FROM saved_billers sb "
                "JOIN billers b ON b.biller_id = sb.biller_id WHERE sb.user_id = ?",
                (user_id, user_id, user_id),
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001 - hotwords are a hint, not a requirement
        return []


@router.post(_ROUTE, response_model=VoiceResponse)
def upload_voice(
    conversation_id: str,
    request: Request,
    user: UserSession,
    audio: Annotated[UploadFile | None, File()] = None,
    consent: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Store one push-to-talk WAV clip, transcribe it with TRTC and append the final turn.

    Errors: ``{"detail": {"code", "message", "audio_id"?, "digest"?, "turn_id"?}}`` with
    400 ``AUDIO_CONSENT_REQUIRED`` / ``AUDIO_MISSING`` / ``AUDIO_EMPTY``; 413
    ``AUDIO_TOO_LARGE`` / ``AUDIO_TOO_LONG`` / ``CONTEXT_LIMIT_RESTATE``; 415
    ``AUDIO_NOT_WAV`` / ``AUDIO_NOT_PCM`` / ``AUDIO_NOT_16BIT`` / ``AUDIO_NOT_MONO`` /
    ``AUDIO_NOT_16KHZ``; 409 ``AUDIO_DUPLICATE``; 422 ``ASR_NO_SPEECH``; 502
    ``ASR_UNAVAILABLE`` / ``ASR_AUTH_FAILED``; 503 ``ASR_UNAVAILABLE`` (not configured,
    nothing stored); 504 ``ASR_TIMEOUT``; 500 ``AUDIO_STORE_FAILED``.
    """
    _check_origin(request)
    store = get_conversation_store()
    if not _owns(store, user.user_id, conversation_id):
        raise _not_found()

    telemetry = get_voice_telemetry()
    with telemetry.workflow_span(
        "ingest",
        session_id=conversation_id,
        user_pseudonym=telemetry.pseudonymize(user.user_id),
        attributes={"conversation_id": conversation_id, "channel": "voice", "route": _ROUTE},
    ) as span:
        trace = (span.trace_id, span.observation_id)
        try:
            result = _ingest(store, user.user_id, conversation_id, audio, consent, trace, span)
        except HTTPException as exc:
            span.set_attributes({"http_status": exc.status_code})
            raise
        result["trace_id"] = trace[0]
        span.set_attributes({"http_status": 200})
        return result


def _ingest(
    store: ConversationStore,
    user_id: str,
    conversation_id: str,
    audio: UploadFile | None,
    consent: str | None,
    trace: tuple[str | None, str | None],
    span: Any,
) -> dict[str, Any]:
    def reject(http_status: int, code: str, message: str, **extra: Any) -> HTTPException:
        span.set_status("rejected", reason_code=code)
        return _fail(http_status, code, message, **extra)

    # 1. Consent and upload checks. Nothing is stored and TRTC is not called.
    if (consent or "").strip().lower() != "true":
        raise reject(
            400, "AUDIO_CONSENT_REQUIRED", "consent=true is required to store the recording"
        )
    if audio is None:
        raise reject(400, "AUDIO_MISSING", "multipart field 'audio' is required")
    try:
        check_content_type(audio.content_type)
        upload = _read_upload(audio)
        wav = validate_wav(upload)
    except AudioRejected as exc:
        raise reject(exc.http_status, exc.code, exc.message) from None

    upload_digest = hashlib.sha256(upload).hexdigest()
    conn = store._conn()
    try:
        duplicate = find_linked_duplicate(conn, conversation_id, upload_digest)
    finally:
        conn.close()
    if duplicate is not None:
        raise reject(
            409,
            "AUDIO_DUPLICATE",
            "this clip already has a transcript turn",
            audio_id=duplicate[0],
            turn_id=duplicate[1],
            digest=upload_digest,
        )

    transcriber = get_transcriber()
    if not transcriber.ready:
        span.set_status("error", reason_code="ASR_UNAVAILABLE")
        raise _fail(503, "ASR_UNAVAILABLE", "speech recognition is not configured; type instead")

    # 2. Store the exact bytes as evidence, then read them back for TRTC.
    retention_until = to_iso(now_utc() + timedelta(days=VOICE_RETENTION_DAYS))
    try:
        stored = store.upload_audio(
            user_id,
            conversation_id,
            upload,
            "audio/wav",
            consent=True,
            retention_until=retention_until,
        )
    except PermissionError:
        raise _not_found() from None
    audio_id = stored["audio_id"]
    try:
        sent_bytes, digest = _read_stored(store, audio_id)
    except (OSError, RuntimeError):
        span.set_status("error", reason_code="AUDIO_STORE_FAILED")
        raise _fail(500, "AUDIO_STORE_FAILED", "stored audio could not be verified") from None
    if digest != upload_digest or sent_bytes != upload:
        span.set_status("error", reason_code="AUDIO_STORE_FAILED")
        raise _fail(500, "AUDIO_STORE_FAILED", "stored audio does not match the upload")

    clip = {"audio_id": audio_id, "sha256_digest": digest}
    _audit(
        store,
        user_id,
        conversation_id,
        "gateway.voice.asr_started",
        {
            **clip,
            "sent_sha256": hashlib.sha256(sent_bytes).hexdigest(),
            "size_bytes": len(sent_bytes),
            "duration_ms": wav.duration_ms,
            "engine": ASR_ENGINE,
            "language": ASR_LANGUAGE,
        },
        trace,
    )

    # 3. Transcribe the stored bytes.
    span.set_output(
        {
            "clip_id": audio_id,
            "clip_duration_ms": wav.duration_ms,
            "size_bytes": len(sent_bytes),
            "engine": ASR_ENGINE,
            "language": ASR_LANGUAGE,
        }
    )

    def asr_failed(exc: AsrError) -> HTTPException:
        _audit(
            store,
            user_id,
            conversation_id,
            "gateway.voice.asr_failed",
            {
                **clip,
                "code": exc.code,
                "provider_code": exc.provider_code,
                "latency_ms": exc.latency_ms,
                "asr_request_id": exc.request_id,
            },
            trace,
        )
        span.set_attributes({"latency_ms": exc.latency_ms, "request_id": exc.request_id or None})
        span.set_status(exc.telemetry_status, reason_code=exc.code)
        message = (
            "no speech was recognised; try again or type your request"
            if exc.code == "ASR_NO_SPEECH"
            else "speech recognition failed; your recording was kept, type your request instead"
        )
        return _fail(
            ASR_HTTP_STATUS.get(exc.code, 502), exc.code, message, audio_id=audio_id, digest=digest
        )

    try:
        asr: AsrResult = transcriber.transcribe(sent_bytes, hotwords=_user_hotwords(store, user_id))
    except AsrError as exc:
        raise asr_failed(exc) from None
    except Exception:  # noqa: BLE001 - an unexpected transcriber error is still a failure
        raise asr_failed(AsrError("ASR_UNAVAILABLE")) from None

    span.set_attributes({"request_id": asr.request_id or None, "latency_ms": asr.latency_ms})
    span.set_output(
        {
            "clip_id": audio_id,
            "clip_duration_ms": wav.duration_ms,
            "audio_duration_ms": asr.audio_duration_ms,
            "size_bytes": len(sent_bytes),
            "transcript_chars": len(asr.text),
            "engine": ASR_ENGINE,
            "language": ASR_LANGUAGE,
        }
    )
    if not asr.text.strip():
        raise asr_failed(
            AsrError("ASR_NO_SPEECH", latency_ms=asr.latency_ms, request_id=asr.request_id or None)
        )

    # 4. One final turn for this clip, linked to its evidence, in one transaction.
    asr_facts = {
        "asr_request_id": asr.request_id or None,
        "latency_ms": asr.latency_ms,
        "audio_duration_ms": asr.audio_duration_ms,
        "engine": ASR_ENGINE,
        "language": ASR_LANGUAGE,
        "transcript_chars": len(asr.text),
    }
    try:
        turn = append_voice_turn(
            store,
            user_id,
            conversation_id,
            audio_id,
            asr.text,
            audit_payload={k: v for k, v in asr_facts.items() if v is not None},
            trace_id=trace[0],
            span_id=trace[1],
        )
    except DuplicateSegment as dup:
        _audit(
            store,
            user_id,
            conversation_id,
            "gateway.voice.turn_rejected",
            {**clip, "code": dup.code, "existing_turn_id": dup.turn_id},
            trace,
        )
        raise reject(
            409,
            dup.code,
            "this clip already has a transcript turn",
            audio_id=dup.audio_id,
            turn_id=dup.turn_id,
            digest=digest,
        ) from None
    except ContextLimitError:
        _audit(
            store,
            user_id,
            conversation_id,
            "gateway.voice.turn_rejected",
            {**clip, "code": ContextLimitError.CODE},
            trace,
        )
        raise reject(
            413,
            ContextLimitError.CODE,
            "context limit exceeded; restate explicitly",
            audio_id=audio_id,
            digest=digest,
        ) from None
    except PermissionError:
        raise _not_found() from None

    span.set_attributes(
        {"turn_id": turn["turn_id"], "conversation_revision": turn["conversation_revision"]}
    )
    span.set_status("ok")
    return {
        "conversation_id": conversation_id,
        "audio_id": audio_id,
        "digest": digest,
        "size_bytes": len(sent_bytes),
        "duration_ms": wav.duration_ms,
        "consent": True,
        "retention_until": retention_until,
        "transcript": asr.text,
        "turn": turn,
        "asr": {
            "request_id": asr.request_id,
            "latency_ms": asr.latency_ms,
            "audio_duration_ms": asr.audio_duration_ms,
            "engine": ASR_ENGINE,
            "language": ASR_LANGUAGE,
        },
    }


# ── GET evidence ─────────────────────────────────────────────────────


@router.get(_ROUTE, response_model=AudioClipList)
def list_voice_clips(conversation_id: str, user: UserSession) -> dict[str, Any]:
    """The owner's audio evidence for this conversation (metadata only)."""
    store = get_conversation_store()
    if not _owns(store, user.user_id, conversation_id):
        raise _not_found()
    conn = store._conn()
    try:
        rows = conn.execute(
            "SELECT audio_id, sha256_digest, content_type, size_bytes, duration_ms, consent, "
            "retention_until, created_at, turn_id FROM audio_evidence "
            "WHERE conversation_id = ? ORDER BY created_at, audio_id",
            (conversation_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "conversation_id": conversation_id,
        "clips": [
            {
                "audio_id": r["audio_id"],
                "digest": r["sha256_digest"],
                "content_type": r["content_type"],
                "size_bytes": int(r["size_bytes"]),
                "duration_ms": r["duration_ms"],
                "consent": bool(r["consent"]),
                "retention_until": r["retention_until"],
                "created_at": r["created_at"],
                "turn_id": r["turn_id"],
            }
            for r in rows
        ],
    }


@router.get(_ROUTE + "/{audio_id}")
def get_voice_clip(conversation_id: str, audio_id: str, user: UserSession) -> Response:
    """The stored WAV bytes for the owner. The digest is re-checked before serving."""
    store = get_conversation_store()
    if not _owns(store, user.user_id, conversation_id):
        raise _not_found()
    conn = store._conn()
    try:
        row = conn.execute(
            "SELECT file_path, sha256_digest, retention_until FROM audio_evidence "
            "WHERE audio_id = ? AND conversation_id = ?",
            (audio_id, conversation_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise _not_found()
    if row["retention_until"] <= to_iso(now_utc()):
        raise _fail(410, "AUDIO_EXPIRED", "the recording is past its retention date")
    try:
        data = Path(row["file_path"]).read_bytes()
    except OSError:
        raise _fail(500, "AUDIO_STORE_FAILED", "stored audio could not be read") from None
    if hashlib.sha256(data).hexdigest() != row["sha256_digest"]:
        raise _fail(500, "AUDIO_INTEGRITY_FAILED", "stored audio does not match its digest")
    return Response(
        content=data,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Content-SHA256": row["sha256_digest"],
            "Content-Disposition": f'inline; filename="{audio_id}.wav"',
        },
    )
