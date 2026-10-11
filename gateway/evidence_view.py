"""The owner's evidence view: "You said → We clarified → You approved → Executed".

:func:`build_evidence` assembles, from gateway records only, every turn of one
conversation (originals kept, corrected ones flagged ``superseded``), every frozen draft
with its latest validation, every payment authorization with its approval evidence,
step-up status and receipt, and the conversation's audit events with the hash-chain
check. Trace links point at the gateway's Langfuse project when telemetry is on.

Never included: audit payloads, envelope bytes or nonces, WebAuthn assertion data or
hashes, step-up codes, salts or verifiers, delegation tokens, session data and audio.
Text (turns, model issue reasons, references) is returned raw; the UI escapes it.

This module is imported only by gateway code.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from gateway.audit import verify_chain
from gateway.clock import utcnow
from gateway.orchestration import leg_views
from gateway.telemetry import trace_urls
from gateway.validation import ValidationRefused, _load_bundle
from ledger.schema import connect
from shared.timeutil import from_iso

__all__ = ["build_evidence"]


def _turns(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT turn_id, seq, role, text, edits_turn_id, answers_question_id, created_at "
        "FROM conversation_turns WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ).fetchall()
    corrected = {r["edits_turn_id"] for r in rows if r["edits_turn_id"]}
    return [
        {
            "turn_id": r["turn_id"],
            "seq": int(r["seq"]),
            "role": r["role"],
            "text": r["text"],
            "edits_turn_id": r["edits_turn_id"],
            "answers_question_id": r["answers_question_id"],
            "created_at": r["created_at"],
            "superseded": r["turn_id"] in corrected,
        }
        for r in rows
    ]


def _validation(conn: sqlite3.Connection, bundle_id: str, links: dict[str, str]) -> dict | None:
    row = conn.execute(
        "SELECT verdict, status, issues_json, model, prompt_hash, trace_id "
        "FROM validation_results WHERE bundle_id = ? ORDER BY attempt DESC LIMIT 1",
        (bundle_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "verdict": row["verdict"],
        "status": row["status"],
        "issues": json.loads(row["issues_json"]) if row["issues_json"] else [],
        "model": row["model"],
        "prompt_hash": row["prompt_hash"],
        "trace_url": links.get(row["trace_id"] or ""),
    }


def _drafts(
    conn: sqlite3.Connection, user_id: str, conversation_id: str, links: dict[str, str]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT bundle_id, owner_user_id, conversation_id, conversation_revision, "
        "canonical_bytes, bundle_hash, status, created_at FROM frozen_drafts "
        "WHERE conversation_id = ? AND owner_user_id = ? ORDER BY created_at, rowid",
        (conversation_id, user_id),
    ).fetchall()
    out = []
    for row in rows:
        try:
            legs = leg_views(conn, user_id, _load_bundle(row))
        except ValidationRefused:  # stored bytes fail their own hash: show no legs
            legs = []
        out.append(
            {
                "bundle_id": row["bundle_id"],
                "bundle_hash": row["bundle_hash"],
                "status": row["status"],
                "created_at": row["created_at"],
                "legs": legs,
                "validation": _validation(conn, row["bundle_id"], links),
            }
        )
    return out


def _step_up(conn: sqlite3.Connection, authorization_id: str, authz_status: str) -> dict | None:
    row = conn.execute(
        "SELECT status, failed_attempts, expires_at FROM step_up_challenges "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    if row is None:
        return None
    status = row["status"]
    if status == "PENDING" and from_iso(row["expires_at"]) <= utcnow():
        status = "EXPIRED"
    elif status == "PENDING" and authz_status != "AWAITING_STEP_UP":
        status = "INVALIDATED"
    return {"status": status, "attempts": int(row["failed_attempts"])}


def _authorizations(
    conn: sqlite3.Connection, user_id: str, conversation_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT authorization_id, bundle_id, status, status_reason, envelope_hash, "
        "issued_at, expires_at FROM authorizations WHERE conversation_id = ? "
        "AND user_id = ? AND operation = 'execute_bundle' ORDER BY created_at, rowid",
        (conversation_id, user_id),
    ).fetchall()
    out = []
    for row in rows:
        authz_id = row["authorization_id"]
        approval = conn.execute(
            "SELECT credential_id, verified_at FROM approval_evidence WHERE authorization_id = ?",
            (authz_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT receipt_json FROM receipts WHERE authorization_id = ? AND user_id = ?",
            (authz_id, user_id),
        ).fetchone()
        out.append(
            {
                "authorization_id": authz_id,
                "bundle_id": row["bundle_id"],
                "status": row["status"],
                "status_reason": row["status_reason"],
                "envelope_hash": row["envelope_hash"],
                "issued_at": row["issued_at"],
                "expires_at": row["expires_at"],
                # Approval evidence exists only for a verified, user-verified assertion.
                "approval": (
                    {
                        "credential_id": approval["credential_id"],
                        "verified_at": approval["verified_at"],
                        "user_verified": True,
                    }
                    if approval is not None
                    else None
                ),
                "step_up": _step_up(conn, authz_id, row["status"]),
                "receipt": json.loads(receipt["receipt_json"]) if receipt is not None else None,
            }
        )
    return out


def build_evidence(db_path: str, user_id: str, conversation_id: str) -> dict[str, Any] | None:
    """The evidence view of the user's own conversation, or None (unknown / not theirs)."""
    conn = connect(db_path)
    try:
        conv = conn.execute(
            "SELECT owner_user_id, revision FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if conv is None or conv["owner_user_id"] != user_id:
            return None
        events = conn.execute(
            "SELECT seq, ts, source, step, trace_id FROM audit_events "
            "WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        validation_traces = conn.execute(
            "SELECT v.trace_id FROM validation_results v JOIN frozen_drafts d "
            "ON d.bundle_id = v.bundle_id WHERE d.conversation_id = ? AND d.owner_user_id = ? "
            "ORDER BY v.created_at",
            (conversation_id, user_id),
        ).fetchall()
        links = trace_urls(
            [e["trace_id"] for e in events] + [v["trace_id"] for v in validation_traces]
        )
        chain = verify_chain(conn, conversation_id=conversation_id)
        return {
            "conversation_id": conversation_id,
            "revision": int(conv["revision"]),
            "turns": _turns(conn, conversation_id),
            "drafts": _drafts(conn, user_id, conversation_id, links),
            "authorizations": _authorizations(conn, user_id, conversation_id),
            "audit": {
                "ok": bool(chain["ok"]),
                "first_bad_seq": chain["first_bad_seq"],
                "events": [
                    {
                        "seq": int(e["seq"]),
                        "ts": e["ts"],
                        "source": e["source"],
                        "step": e["step"],
                        "trace_id": e["trace_id"],
                    }
                    for e in events
                ],
            },
            "trace_urls": list(links.values()),
        }
    finally:
        conn.close()
