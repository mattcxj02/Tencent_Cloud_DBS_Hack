"""Prototype hash-chained audit log.

Each audit row includes:

    seq (INTEGER PK, gapless), ts, source (gateway|agent|validator),
    session_id, conversation_id, user_id, step, payload_json (canonical),
    trace_id, span_id, prev_hash, row_hash.

Genesis ``prev_hash`` = 64 zeros.
``row_hash = SHA256(canonical_bytes(row contents excluding row_hash, including prev_hash))``.

:func:`append_audit` MUST be called inside the caller's open
``BEGIN IMMEDIATE`` write transaction so that gateway financial events
and audit commit atomically.  Reading the tail and inserting happen
under that same lock, ensuring a gapless chain.

:func:`verify_chain` detects tampering with payload, step, or
``prev_hash`` at any row, and detects deletion of middle rows (gap in
``seq``).  Tail truncation and a privileged complete rewrite are NOT
detectable without external checkpoints; see the test docstring for
details.

Supersession: :func:`supersede_for_conversation` marks pending records
bound to older conversation revisions as ``SUPERSEDED``.  Later tasks
register their tables in :data:`REVISION_BOUND_TABLES`.

This module is imported only by gateway code.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from shared.canonical import canonical_bytes
from shared.timeutil import now_utc, to_iso

__all__ = [
    "append_audit",
    "verify_chain",
    "supersede_for_conversation",
    "REVISION_BOUND_TABLES",
    "GENESIS_HASH",
    "ALLOWED_AGENT_STEPS",
    "FORBIDDEN_STEP_PREFIXES",
]

#: Genesis previous-hash: 64 zero hex characters.
GENESIS_HASH = "0" * 64

#: Allowlist of agent step names (prefix ``agent.``).
ALLOWED_AGENT_STEPS: frozenset[str] = frozenset(
    {
        "agent.extract",
        "agent.normalize",
        "agent.resolve",
        "agent.clarify",
        "agent.propose",
    }
)

#: Step prefixes that are gateway-trusted and must never come from agent.
FORBIDDEN_STEP_PREFIXES: tuple[str, ...] = (
    "gateway.",
    "approval.",
    "execution.",
)

#: Registry of tables that carry revision-bound pending records.
#:
#: Each entry maps table name to a dict with:
#:   - ``revision_column``: the column holding the conversation revision
#:   - ``status_column``: the column holding the status (PENDING, etc.)
#:   - ``conversation_id_column``: the column for conversation_id
#:   - ``superseded_value``: the value to set for superseded records
#:
#: T-05 registers frozen_drafts and authorizations.
REVISION_BOUND_TABLES: dict[str, dict[str, str]] = {
    "frozen_drafts": {
        "revision_column": "conversation_revision",
        "status_column": "status",
        "conversation_id_column": "conversation_id",
        "superseded_value": "SUPERSEDED",
    },
    "authorizations": {
        "revision_column": "conversation_revision",
        "status_column": "status",
        "conversation_id_column": "conversation_id",
        "superseded_value": "SUPERSEDED",
    },
}


def _row_contents_for_hash(
    seq: int,
    ts: str,
    source: str,
    session_id: str | None,
    conversation_id: str | None,
    user_id: str | None,
    step: str,
    payload_json: str,
    trace_id: str | None,
    span_id: str | None,
    prev_hash: str,
) -> bytes:
    """Compute the canonical bytes that feed into ``row_hash``.

    ``row_hash`` excludes itself but includes ``prev_hash``.
    """
    contents = {
        "seq": seq,
        "ts": ts,
        "source": source,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "step": step,
        "payload_json": payload_json,
        "trace_id": trace_id,
        "span_id": span_id,
        "prev_hash": prev_hash,
    }
    return canonical_bytes(contents)


def append_audit(
    conn: sqlite3.Connection,
    *,
    source: str,
    step: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> int:
    """Append an audit row inside the caller's ``BEGIN IMMEDIATE`` transaction.

    Reads the tail (max seq + prev_hash) under the same lock, computes
    the new ``row_hash``, and inserts.  Returns the new ``seq``.

    **Must not be called outside a write transaction.**  The caller
    opens ``BEGIN IMMEDIATE``, writes its data + audit, then commits or
    rolls back together.
    """
    if not conn.in_transaction:
        raise RuntimeError("append_audit must run inside the caller's BEGIN IMMEDIATE transaction")
    ts = to_iso(now_utc())

    # Canonical payload JSON: sorted keys, Money as 2-dp strings, floats rejected.
    payload_json = canonical_bytes(payload).decode("utf-8")

    # Read the tail under the same lock.
    tail = conn.execute(
        "SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if tail is None:
        prev_hash = GENESIS_HASH
        next_seq = 1
    else:
        prev_hash = tail["row_hash"]
        next_seq = int(tail["seq"]) + 1

    row_bytes = _row_contents_for_hash(
        next_seq,
        ts,
        source,
        session_id,
        conversation_id,
        user_id,
        step,
        payload_json,
        trace_id,
        span_id,
        prev_hash,
    )
    row_hash = hashlib.sha256(row_bytes).hexdigest()

    conn.execute(
        """
        INSERT INTO audit_events
            (seq, ts, source, session_id, conversation_id, user_id,
             step, payload_json, trace_id, span_id, prev_hash, row_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            next_seq,
            ts,
            source,
            session_id,
            conversation_id,
            user_id,
            step,
            payload_json,
            trace_id,
            span_id,
            prev_hash,
            row_hash,
        ),
    )
    return next_seq


def verify_chain(
    conn: sqlite3.Connection,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Verify the hash chain integrity.

    If *conversation_id* is provided, only rows for that conversation
    are verified.  Each row's ``row_hash`` is recomputed and checked
    against the stored value (detects payload/step/prev_hash tampering).
    For the global chain (no filter), seq continuity is also checked
    (detects middle-row deletion).

    Returns ``{"ok": bool, "first_bad_seq": int | None}``.

    Detects:
        - Tampering with ``payload_json``, ``step``, or ``prev_hash``.
        - Deletion of a middle row (gap in ``seq``) — only when
          *conversation_id* is None (full chain).

    Does NOT detect (documented limitation):
        - Tail truncation (rows deleted from the end).
        - A privileged complete rewrite (all rows + hashes rewritten
          consistently).  External checkpoints are needed for that;
          they are a documented future enhancement, not implemented here.
    """
    if conversation_id is not None:
        # Conversation-scoped: verify each row's hash independently.
        # The global chain links all events, so a filtered subset may
        # have non-contiguous seqs.  We recompute row_hash for each row
        # and also verify that the global predecessor's row_hash matches
        # the stored prev_hash (fetched from the full table if needed).
        rows = conn.execute(
            """
            SELECT seq, ts, source, session_id, conversation_id, user_id,
                   step, payload_json, trace_id, span_id, prev_hash, row_hash
            FROM audit_events
            WHERE conversation_id = ?
            ORDER BY seq
            """,
            (conversation_id,),
        ).fetchall()

        for row in rows:
            seq = int(row["seq"])
            # Verify the predecessor link: prev_hash should be either
            # genesis or the row_hash of seq-1 (the global predecessor).
            prev_seq = seq - 1
            if prev_seq == 0:
                expected_prev = GENESIS_HASH
            else:
                prev_row = conn.execute(
                    "SELECT row_hash FROM audit_events WHERE seq = ?",
                    (prev_seq,),
                ).fetchone()
                if prev_row is None:
                    # Predecessor deleted — chain broken.
                    return {"ok": False, "first_bad_seq": seq}
                expected_prev = prev_row["row_hash"]

            if row["prev_hash"] != expected_prev:
                return {"ok": False, "first_bad_seq": seq}

            # Recompute row_hash.
            row_bytes = _row_contents_for_hash(
                seq,
                row["ts"],
                row["source"],
                row["session_id"],
                row["conversation_id"],
                row["user_id"],
                row["step"],
                row["payload_json"],
                row["trace_id"],
                row["span_id"],
                row["prev_hash"],
            )
            expected_row_hash = hashlib.sha256(row_bytes).hexdigest()
            if row["row_hash"] != expected_row_hash:
                return {"ok": False, "first_bad_seq": seq}

        return {"ok": True, "first_bad_seq": None}

    # Full-chain verification: check seq continuity + hash chain.
    rows = conn.execute(
        """
        SELECT seq, ts, source, session_id, conversation_id, user_id,
               step, payload_json, trace_id, span_id, prev_hash, row_hash
        FROM audit_events
        ORDER BY seq
        """,
    ).fetchall()

    expected_prev = GENESIS_HASH
    expected_seq = None  # set after first row

    for row in rows:
        seq = int(row["seq"])

        # Check seq continuity (gapless).
        if expected_seq is None:
            expected_seq = seq
        else:
            if seq != expected_seq + 1:
                return {"ok": False, "first_bad_seq": seq}
            expected_seq = seq

        # Check prev_hash chain.
        if row["prev_hash"] != expected_prev:
            return {"ok": False, "first_bad_seq": seq}

        # Recompute row_hash.
        row_bytes = _row_contents_for_hash(
            seq,
            row["ts"],
            row["source"],
            row["session_id"],
            row["conversation_id"],
            row["user_id"],
            row["step"],
            row["payload_json"],
            row["trace_id"],
            row["span_id"],
            row["prev_hash"],
        )
        expected_row_hash = hashlib.sha256(row_bytes).hexdigest()

        if row["row_hash"] != expected_row_hash:
            return {"ok": False, "first_bad_seq": seq}

        expected_prev = row["row_hash"]

    return {"ok": True, "first_bad_seq": None}


# ── Supersession ────────────────────────────────────────────────────


def supersede_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    new_revision: int,
) -> int:
    """Mark pending records bound to older revisions as SUPERSEDED.

    Iterates :data:`REVISION_BOUND_TABLES` and, for each registered
    table, updates rows where:
        - ``conversation_id_column == conversation_id``
        - ``revision_column < new_revision``
        - ``status_column`` is a pending status (PENDING, PENDING_APPROVAL,
          AWAITING_STEP_UP)

    Returns the total number of rows superseded.

    Also audits the supersession event.
    """
    total = 0
    pending_statuses = (
        "PENDING",
        "CURRENT",  # validation_results: a correction supersedes the latest verdict now
        "DRAFT_FROZEN",
        "PENDING_APPROVAL",
        "AWAITING_STEP_UP",
    )

    for table_name, cols in REVISION_BOUND_TABLES.items():
        status_col = cols["status_column"]
        rev_col = cols["revision_column"]
        conv_col = cols["conversation_id_column"]
        superseded_value = cols["superseded_value"]

        # Build placeholders for pending statuses.
        placeholders = ",".join("?" for _ in pending_statuses)
        cur = conn.execute(
            f"""
            UPDATE {table_name}
            SET {status_col} = ?
            WHERE {conv_col} = ?
              AND {rev_col} < ?
              AND {status_col} IN ({placeholders})
            """,
            (
                superseded_value,
                conversation_id,
                new_revision,
                *pending_statuses,
            ),
        )
        total += cur.rowcount

    # Audit the supersession (if any records were superseded).
    if total > 0:
        append_audit(
            conn,
            source="gateway",
            conversation_id=conversation_id,
            step="gateway.supersede",
            payload={
                "conversation_id": conversation_id,
                "new_revision": new_revision,
                "superseded_count": total,
            },
        )

    return total
