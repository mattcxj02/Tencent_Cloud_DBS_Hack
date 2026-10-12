"""Gateway-owned conversation store.

The gateway is the sole authority for conversation IDs, turn IDs, turn
ordering, revisions and hashes.  Original turns are never deleted or
rewritten; corrections create new turns with ``edits_turn_id`` pointing
to the original.

Agent service credentials can record ``app_question`` turns (via the
internal route) but can **never** create ``user_*`` roles.  There is no
internal route to append user turns.

All user-originated appends bump ``conversation.revision`` and trigger
supersession of pending records bound to older revisions.  Both happen
inside the same ``BEGIN IMMEDIATE`` transaction as the audit append.

This module is imported only by gateway code.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any

from shared.canonical import canonical_bytes
from shared.clarification import QuestionField, Selection
from shared.timeutil import now_utc, to_iso

__all__ = [
    "ConversationStore",
    "MAX_TEXT_PER_TURN",
    "MAX_TURNS_PER_CONVERSATION",
    "MAX_TOTAL_CHARS_PER_CONVERSATION",
    "MAX_AUDIO_SIZE_BYTES",
    "ALLOWED_AUDIO_CONTENT_TYPE",
    "DEFAULT_AUDIO_DIR",
    "ContextLimitError",
    "compute_conversation_hash",
]

#: Maximum text length per turn (characters).
MAX_TEXT_PER_TURN = 2000

#: Maximum turns per conversation.
MAX_TURNS_PER_CONVERSATION = 40

#: Maximum total characters across all turns in a conversation.
MAX_TOTAL_CHARS_PER_CONVERSATION = 20000

#: Maximum audio upload size (3 MB — TRTC sentence limit).
MAX_AUDIO_SIZE_BYTES = 3 * 1024 * 1024

#: Only accepted audio content type for now.
ALLOWED_AUDIO_CONTENT_TYPE = "audio/wav"

#: Default directory for raw audio evidence storage (gateway-only).
DEFAULT_AUDIO_DIR = "data/audio/"

#: SHA-256 of an empty string — used for the genesis conversation hash.
_EMPTY_HASH = "0" * 64


class ContextLimitError(Exception):
    """Raised when the conversation exceeds the supported context budget.

    The caller must ask the user for an explicit restatement rather than
    silently truncating.  Carries the stable error code
    ``CONTEXT_LIMIT_RESTATE``.
    """

    CODE = "CONTEXT_LIMIT_RESTATE"


# ── Hash computation ────────────────────────────────────────────────


def compute_conversation_hash(
    conversation_id: str,
    revision: int,
    turns: list[dict[str, Any]],
) -> str:
    """Compute the canonical conversation hash.

    ``conversation_hash = SHA256(canonical_bytes({
        conversation_id, revision, turns: [
            {turn_id, seq, role, text, edits_turn_id,
             answers_question_id, created_at}, ...
        ]
    }))``

    The turn list is ordered by ``seq``.  Array order is meaningful and
    preserved by canonical serialization.
    """
    payload = {
        "conversation_id": conversation_id,
        "revision": revision,
        "turns": turns,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


# ── Store ───────────────────────────────────────────────────────────


class ConversationStore:
    """Read/write access to the conversation and audio-evidence tables.

    All write methods open a ``BEGIN IMMEDIATE`` transaction, perform the
    append + revision bump + supersession + audit, and commit atomically.
    """

    def __init__(self, db_path: str, audio_dir: str = DEFAULT_AUDIO_DIR) -> None:
        self._db_path = db_path
        self._audio_dir = audio_dir

    def _conn(self) -> sqlite3.Connection:
        from ledger.schema import connect

        return connect(self._db_path)

    # ── Conversation lifecycle ──────────────────────────────────────

    def create_conversation(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """Create a new conversation owned by *user_id*.

        Returns the conversation dict with ``conversation_id``, ``revision``
        (0), ``conversation_hash``, and timestamps.
        """
        conversation_id = f"conv-{uuid.uuid4().hex[:16]}"
        now = now_utc()
        ts = to_iso(now)

        # Genesis hash: hash of empty turn list at revision 0.
        conv_hash = compute_conversation_hash(conversation_id, 0, [])

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO conversations
                    (conversation_id, owner_user_id, revision,
                     conversation_hash, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (conversation_id, user_id, conv_hash, ts, ts),
            )
            # Audit the conversation creation.
            from gateway.audit import append_audit

            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=user_id,
                step="gateway.conversation.create",
                payload={"conversation_id": conversation_id, "revision": 0},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            "conversation_id": conversation_id,
            "owner_user_id": user_id,
            "revision": 0,
            "conversation_hash": conv_hash,
            "created_at": ts,
            "updated_at": ts,
            "turns": [],
        }

    def _get_conversation_row(
        self, conn: sqlite3.Connection, conversation_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()

    def _get_turns(self, conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
        """Return turn dicts (only the fields needed for responses)."""
        rows = conn.execute(
            """
            SELECT turn_id, seq, role, text,
                   edits_turn_id, answers_question_id, created_at
            FROM conversation_turns
            WHERE conversation_id = ?
            ORDER BY seq
            """,
            (conversation_id,),
        ).fetchall()
        return [
            {
                "turn_id": r["turn_id"],
                "seq": int(r["seq"]),
                "role": r["role"],
                "text": r["text"],
                "edits_turn_id": r["edits_turn_id"],
                "answers_question_id": r["answers_question_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def _next_seq(self, conn: sqlite3.Connection, conversation_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _check_bounds(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        text: str,
    ) -> None:
        """Validate text length, turn count and total character budget."""
        if len(text) > MAX_TEXT_PER_TURN:
            raise ContextLimitError(f"text exceeds {MAX_TEXT_PER_TURN} chars per turn")

        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(LENGTH(text)), 0) AS total "
            "FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        turn_count = int(row["cnt"])
        total_chars = int(row["total"])

        if turn_count >= MAX_TURNS_PER_CONVERSATION:
            raise ContextLimitError(f"conversation exceeds {MAX_TURNS_PER_CONVERSATION} turns")
        if total_chars + len(text) > MAX_TOTAL_CHARS_PER_CONVERSATION:
            raise ContextLimitError(
                f"conversation exceeds {MAX_TOTAL_CHARS_PER_CONVERSATION} total chars"
            )

    def _append_turn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        role: str,
        text: str,
        *,
        edits_turn_id: str | None = None,
        answers_question_id: str | None = None,
        bump_revision: bool = True,
    ) -> dict[str, Any]:
        """Append a turn inside the caller's transaction.

        When *bump_revision* is True (user-originated turns), the
        conversation revision is incremented, the hash is recomputed,
        supersession runs, and the change is audited.

        When *bump_revision* is False (``app_question`` from agent), no
        revision bump, no supersession, no user-visible hash change.
        """
        conv_row = self._get_conversation_row(conn, conversation_id)
        if conv_row is None:
            raise ValueError(f"conversation not found: {conversation_id}")

        seq = self._next_seq(conn, conversation_id)
        turn_id = f"turn-{uuid.uuid4().hex[:16]}"
        ts = to_iso(now_utc())

        conn.execute(
            """
            INSERT INTO conversation_turns
                (turn_id, conversation_id, seq, role, text,
                 edits_turn_id, answers_question_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (turn_id, conversation_id, seq, role, text, edits_turn_id, answers_question_id, ts),
        )

        if bump_revision:
            new_revision = int(conv_row["revision"]) + 1
            turns = self._get_turns(conn, conversation_id)
            new_hash = compute_conversation_hash(conversation_id, new_revision, turns)

            conn.execute(
                """
                UPDATE conversations
                SET revision = ?, conversation_hash = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (new_revision, new_hash, ts, conversation_id),
            )

            # Supersede pending records bound to older revisions.
            from gateway.audit import supersede_for_conversation

            supersede_for_conversation(conn, conversation_id, new_revision)

            # Audit the revision bump.
            from gateway.audit import append_audit

            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=conv_row["owner_user_id"],
                step="gateway.conversation.revision",
                payload={
                    "revision": new_revision,
                    "turn_id": turn_id,
                    "role": role,
                },
            )

            return {
                "turn_id": turn_id,
                "seq": seq,
                "role": role,
                "text": text,
                "edits_turn_id": edits_turn_id,
                "answers_question_id": answers_question_id,
                "created_at": ts,
                "conversation_revision": new_revision,
                "conversation_hash": new_hash,
            }

        # app_question — no revision bump, no supersession.
        return {
            "turn_id": turn_id,
            "seq": seq,
            "role": role,
            "text": text,
            "edits_turn_id": edits_turn_id,
            "answers_question_id": answers_question_id,
            "created_at": ts,
        }

    # ── User-originated appends (all bump revision) ─────────────────

    def append_text_turn(self, user_id: str, conversation_id: str, text: str) -> dict[str, Any]:
        """Append a ``user_text`` turn."""
        return self._user_append(user_id, conversation_id, "user_text", text)

    def append_asr_final_turn(
        self, user_id: str, conversation_id: str, text: str
    ) -> dict[str, Any]:
        """Append a ``user_asr_final`` turn (final ASR transcript)."""
        return self._user_append(user_id, conversation_id, "user_asr_final", text)

    def append_transcript_edit(
        self,
        user_id: str,
        conversation_id: str,
        edits_turn_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Append a ``user_transcript_edit`` turn correcting a prior turn.

        The original turn is retained; the new turn records the corrected
        text and ``edits_turn_id`` points to the original.
        """
        return self._user_append(
            user_id,
            conversation_id,
            "user_transcript_edit",
            text,
            edits_turn_id=edits_turn_id,
        )

    def append_answer(
        self,
        user_id: str,
        conversation_id: str,
        answers_question_id: str,
        text: str | None,
        *,
        question_revision: int | None = None,
        selections: list[Selection] | None = None,
    ) -> dict[str, Any]:
        """Append a ``user_answer`` turn tied to an ``app_question``."""
        if selections:
            if text is not None:
                raise ValueError("CHOICE_WITH_FREE_TEXT")
            from gateway.telemetry import stage

            with stage(
                "ingest",
                user_id=user_id,
                conversation_id=conversation_id,
                attributes={"conversation_id": conversation_id, "channel": "user_answer"},
            ) as span:
                result = self._append_selection(
                    user_id, conversation_id, answers_question_id, question_revision, selections
                )
                span.set_attributes(
                    {
                        "turn_id": result["turn_id"],
                        "turn_index": result["seq"],
                        "conversation_revision": result["conversation_revision"],
                    }
                )
                return result
        if text is None or not text.strip():
            raise ValueError("ANSWER_EMPTY")
        return self._user_append(
            user_id,
            conversation_id,
            "user_answer",
            text,
            answers_question_id=answers_question_id,
        )

    def _append_selection(
        self,
        user_id: str,
        conversation_id: str,
        question_id: str,
        question_revision: int | None,
        selections: list[Selection],
    ) -> dict[str, Any]:
        from gateway import clarifications as C
        from gateway.audit import append_audit

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conv = self._get_conversation_row(conn, conversation_id)
            if conv is None or conv["owner_user_id"] != user_id:
                raise PermissionError("conversation not owned by user")
            q = C.check_question(conn, conversation_id, question_id, int(conv["revision"]))
            if q is None:
                raise ValueError("CHOICE_QUESTION_INVALID")
            text, chosen = C.selected_answer(conn, user_id, q, question_revision, selections)
            self._check_bounds(conn, conversation_id, text)
            result = self._append_turn(
                conn, conversation_id, "user_answer", text, answers_question_id=question_id
            )
            for s in chosen:
                s["span"]["turn_id"] = result["turn_id"]
            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=user_id,
                step="gateway.clarification.answer",
                payload={
                    "question_id": question_id,
                    "question_revision": q["revision"],
                    "revision": result["conversation_revision"],
                    "turn_id": result["turn_id"],
                    "selections": chosen,
                },
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _user_append(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        text: str,
        *,
        edits_turn_id: str | None = None,
        answers_question_id: str | None = None,
    ) -> dict[str, Any]:
        """Common path for user-originated turns.

        Opens BEGIN IMMEDIATE, checks ownership + bounds, appends the turn,
        bumps revision, supersedes, audits, commits. Each append is one ``ingest``
        workflow span (a fresh trace in the conversation's session); its trace ID is
        stored on the revision audit row. Turn text is never sent to telemetry.
        """
        from gateway.telemetry import stage

        with stage(
            "ingest",
            user_id=user_id,
            conversation_id=conversation_id,
            attributes={"conversation_id": conversation_id, "channel": role},
        ) as span:
            try:
                result = self._user_append_locked(
                    user_id,
                    conversation_id,
                    role,
                    text,
                    edits_turn_id=edits_turn_id,
                    answers_question_id=answers_question_id,
                )
            except ContextLimitError:
                span.set_status("rejected", reason_code=ContextLimitError.CODE)
                raise
            except (PermissionError, ValueError):
                span.set_status("rejected", reason_code="CONVERSATION_NOT_FOUND")
                raise
            span.set_attributes(
                {
                    "turn_id": result["turn_id"],
                    "turn_index": result["seq"],
                    "conversation_revision": result["conversation_revision"],
                }
            )
            if edits_turn_id is not None:
                # A correction supersedes drafts/approvals bound to the old revision.
                span.set_status("ok", reason_code="TRANSCRIPT_CORRECTED")
            return result

    def _user_append_locked(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        text: str,
        *,
        edits_turn_id: str | None = None,
        answers_question_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conv_row = self._get_conversation_row(conn, conversation_id)
            if conv_row is None:
                raise ValueError(f"conversation not found: {conversation_id}")
            if conv_row["owner_user_id"] != user_id:
                raise PermissionError("conversation not owned by user")

            # If editing a turn, verify the original exists in this conversation.
            if edits_turn_id is not None:
                orig = conn.execute(
                    "SELECT turn_id FROM conversation_turns "
                    "WHERE turn_id = ? AND conversation_id = ?",
                    (edits_turn_id, conversation_id),
                ).fetchone()
                if orig is None:
                    raise ValueError(f"edits_turn_id not found: {edits_turn_id}")

            # If answering a question, verify the question exists.
            if answers_question_id is not None:
                from gateway.clarifications import check_question

                check_question(
                    conn, conversation_id, answers_question_id, int(conv_row["revision"])
                )
                q = conn.execute(
                    "SELECT turn_id, role FROM conversation_turns "
                    "WHERE turn_id = ? AND conversation_id = ?",
                    (answers_question_id, conversation_id),
                ).fetchone()
                if q is None:
                    raise ValueError(f"answers_question_id not found: {answers_question_id}")
                if q["role"] != "app_question":
                    raise ValueError(f"turn {answers_question_id} is not an app_question")

            self._check_bounds(conn, conversation_id, text)

            result = self._append_turn(
                conn,
                conversation_id,
                role,
                text,
                edits_turn_id=edits_turn_id,
                answers_question_id=answers_question_id,
                bump_revision=True,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Agent-originated (app_question, no revision bump) ───────────

    def append_question(
        self,
        conversation_id: str,
        text: str,
        *,
        revision: int | None = None,
        fields: list[QuestionField] | None = None,
        extraction: dict | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an ``app_question`` turn (does NOT bump user revision).

        Called by the internal route after the delegation token has been
        verified.  The text may originate from an agent proposal but is
        labelled as application output.
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            from gateway.drafting_work import check_run

            check_run(run_id, conn)
            conv_row = self._get_conversation_row(conn, conversation_id)
            if conv_row is None:
                raise ValueError(f"conversation not found: {conversation_id}")

            from gateway import clarifications as C

            current_revision = int(conv_row["revision"])
            if revision is not None and revision != current_revision:
                raise ValueError("STALE_CONVERSATION_REVISION")
            if fields and revision is None:
                raise ValueError("QUESTION_REVISION_REQUIRED")
            prepared = C.prepare_fields(conn, conv_row["owner_user_id"], fields or [])

            self._check_bounds(conn, conversation_id, text)
            result = self._append_turn(
                conn,
                conversation_id,
                "app_question",
                text,
                bump_revision=False,
            )
            q = {
                "turn_id": result["turn_id"],
                "revision": current_revision,
                "fields": prepared,
                "extraction": extraction,
            }
            C.save_question(conn, conversation_id, conv_row["owner_user_id"], q)
            result["clarification"] = C.public_question(q)
            check_run(run_id, conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Audio evidence ──────────────────────────────────────────────

    def upload_audio(
        self,
        user_id: str,
        conversation_id: str,
        audio_bytes: bytes,
        content_type: str,
        *,
        consent: bool = False,
        retention_until: str | None = None,
        turn_id: str | None = None,
        audio_dir: str | None = None,
    ) -> dict[str, Any]:
        """Store raw audio bytes as evidence.

        Accepts only ``audio/wav`` up to :data:`MAX_AUDIO_SIZE_BYTES`.
        Stores the file under a gateway-only directory, recording the
        SHA-256 digest, content type, size, WAV duration (if parseable),
        consent flag and retention-until.
        """
        if content_type != ALLOWED_AUDIO_CONTENT_TYPE:
            raise ValueError(
                f"unsupported content type: {content_type!r}; "
                f"expected {ALLOWED_AUDIO_CONTENT_TYPE!r}"
            )
        if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
            raise ValueError(f"audio exceeds {MAX_AUDIO_SIZE_BYTES} bytes")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conv_row = self._get_conversation_row(conn, conversation_id)
            if conv_row is None:
                raise ValueError(f"conversation not found: {conversation_id}")
            if conv_row["owner_user_id"] != user_id:
                raise PermissionError("conversation not owned by user")

            audio_id = f"aud-{uuid.uuid4().hex[:16]}"
            ts = to_iso(now_utc())
            digest = hashlib.sha256(audio_bytes).hexdigest()

            if retention_until is None:
                # Default retention: 90 days from upload.
                from datetime import timedelta

                retention_until = to_iso(now_utc() + timedelta(days=90))

            # Parse WAV header for duration if possible.
            duration_ms = _parse_wav_duration_ms(audio_bytes)

            directory = audio_dir or self._audio_dir
            file_path = f"{directory}{audio_id}.wav"

            # Write the file to disk.
            import os

            os.makedirs(directory, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(audio_bytes)

            conn.execute(
                """
                INSERT INTO audio_evidence
                    (audio_id, conversation_id, turn_id, file_path,
                     sha256_digest, content_type, size_bytes, duration_ms,
                     consent, retention_until, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audio_id,
                    conversation_id,
                    turn_id,
                    file_path,
                    digest,
                    content_type,
                    len(audio_bytes),
                    duration_ms,
                    1 if consent else 0,
                    retention_until,
                    ts,
                ),
            )

            # Audit the audio upload (store digest/id only, never raw audio).
            from gateway.audit import append_audit

            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=user_id,
                step="gateway.audio.upload",
                payload={
                    "audio_id": audio_id,
                    "sha256_digest": digest,
                    "size_bytes": len(audio_bytes),
                    "content_type": content_type,
                },
            )

            conn.commit()
            return {
                "audio_id": audio_id,
                "conversation_id": conversation_id,
                "sha256_digest": digest,
                "content_type": content_type,
                "size_bytes": len(audio_bytes),
                "duration_ms": duration_ms,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Read methods ────────────────────────────────────────────────

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        """Return a conversation with all turns, if owned by *user_id*."""
        conn = self._conn()
        try:
            conv_row = self._get_conversation_row(conn, conversation_id)
            if conv_row is None or conv_row["owner_user_id"] != user_id:
                return None
            turns = self._get_turns(conn, conversation_id)
            return {
                "conversation_id": conv_row["conversation_id"],
                "owner_user_id": conv_row["owner_user_id"],
                "revision": int(conv_row["revision"]),
                "conversation_hash": conv_row["conversation_hash"],
                "created_at": conv_row["created_at"],
                "updated_at": conv_row["updated_at"],
                "turns": turns,
            }
        finally:
            conn.close()

    def get_snapshot(self, conversation_id: str) -> dict[str, Any] | None:
        """Return ordered turns + revision + hash (for internal agent use).

        Does NOT check ownership here; the route enforces that the
        delegation token's user_id owns the conversation.
        """
        conn = self._conn()
        try:
            conv_row = self._get_conversation_row(conn, conversation_id)
            if conv_row is None:
                return None
            turns = self._get_turns(conn, conversation_id)
            from gateway.clarifications import structured_update

            return {
                "conversation_id": conv_row["conversation_id"],
                "revision": int(conv_row["revision"]),
                "conversation_hash": conv_row["conversation_hash"],
                "turns": turns,
                "structured_update": structured_update(
                    conn, conversation_id, int(conv_row["revision"])
                ),
            }
        finally:
            conn.close()

    def conversation_owner(self, conn: sqlite3.Connection, conversation_id: str) -> str | None:
        """Return the owner_user_id of a conversation, or None."""
        row = self._get_conversation_row(conn, conversation_id)
        if row is None:
            return None
        return row["owner_user_id"]


# ── WAV duration parsing ────────────────────────────────────────────


def _parse_wav_duration_ms(data: bytes) -> int | None:
    """Parse a WAV header and return duration in milliseconds.

    Returns None if the header cannot be parsed.  Supports standard
    PCM WAV files with the canonical RIFF/WAVE/fmt/data chunk structure.
    """
    if len(data) < 44:
        return None
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    # Find the fmt chunk and data chunk.
    offset = 12
    fmt_found = False
    sample_rate = 0
    channels = 0
    bits_per_sample = 0
    data_size = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if chunk_id == b"fmt ":
            if offset + 24 > len(data):
                return None
            channels = int.from_bytes(data[offset + 10 : offset + 12], "little")
            sample_rate = int.from_bytes(data[offset + 12 : offset + 16], "little")
            bits_per_sample = int.from_bytes(data[offset + 22 : offset + 24], "little")
            fmt_found = True
        elif chunk_id == b"data":
            data_size = chunk_size
            break
        # Chunks are word-aligned; pad to even.
        offset += 8 + chunk_size
        if chunk_size % 2 != 0:
            offset += 1

    if not fmt_found or sample_rate == 0 or channels == 0 or bits_per_sample == 0:
        return None
    if data_size == 0:
        return None
    bytes_per_sample = bits_per_sample // 8
    if bytes_per_sample == 0:
        return None
    total_samples = data_size // (bytes_per_sample * channels)
    duration_ms = int(total_samples * 1000 / sample_rate)
    return duration_ms
