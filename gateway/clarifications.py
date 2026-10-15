"""Gateway-owned questions and selections, persisted in the mandatory audit log.

No database upgrade is needed. Question context is an untrusted extraction checkpoint;
only a session-authenticated answer creates selection evidence. The gateway rechecks
ownership and the field/leg/revision binding again when freezing a proposal.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal
from typing import Any

from gateway.audit import append_audit
from shared.canonical import canonical_bytes
from shared.clarification import QuestionField, Selection
from shared.schemas import TargetType


def events(conn: sqlite3.Connection, conversation_id: str, step: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM audit_events WHERE conversation_id = ? "
        "AND source = 'gateway' AND step = ? ORDER BY seq",
        (conversation_id, step),
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def question(conn: sqlite3.Connection, conversation_id: str, turn_id: str) -> dict | None:
    return next(
        (
            e
            for e in reversed(events(conn, conversation_id, "gateway.clarification.question"))
            if e["turn_id"] == turn_id
        ),
        None,
    )


def record_label(
    conn: sqlite3.Connection, user_id: str, target_type: TargetType, record_id: str
) -> str:
    """Resolve IDs independently of the agent; all choices are owned bank records."""
    if target_type == TargetType.own_account:
        row = conn.execute(
            "SELECT a.type, a.masked_suffix FROM accounts a JOIN account_holders h "
            "ON h.account_id = a.account_id WHERE h.user_id = ? AND a.account_id = ?",
            (user_id, record_id),
        ).fetchone()
        if row:
            return f"{row['type']} (account ending {row['masked_suffix']})"
    elif target_type in (TargetType.saved_payee, TargetType.saved_paynow):
        kind = "paynow" if target_type == TargetType.saved_paynow else "bank"
        row = conn.execute(
            "SELECT display_name, masked_suffix FROM payees WHERE owner_user_id = ? "
            "AND payee_id = ? AND kind = ?",
            (user_id, record_id, kind),
        ).fetchone()
        if row:
            if kind == "paynow":
                return f"{row['display_name']} (PayNow)"
            suffix = f" (account ending {row['masked_suffix']})" if row["masked_suffix"] else ""
            return row["display_name"] + suffix
    elif target_type == TargetType.biller:
        row = conn.execute(
            "SELECT b.display_name FROM billers b JOIN saved_billers s "
            "ON s.biller_id = b.biller_id WHERE s.user_id = ? AND b.biller_id = ?",
            (user_id, record_id),
        ).fetchone()
        if row:
            return row["display_name"]
    elif target_type == TargetType.owned_card:
        row = conn.execute(
            "SELECT masked_suffix FROM cards WHERE owner_user_id = ? AND card_id = ?",
            (user_id, record_id),
        ).fetchone()
        if row:
            return f"card (ending {row['masked_suffix']})"
    elif target_type == TargetType.sgx_symbol:
        from gateway.evidence import SGX_NAMES

        if record_id in SGX_NAMES:
            return record_id
    raise ValueError("CHOICE_RECORD_INVALID")


def prepare_fields(
    conn: sqlite3.Connection, user_id: str, fields: list[QuestionField]
) -> list[dict]:
    result = []
    seen = set()
    for f in fields:
        key = (f.leg_id, f.field)
        if key in seen:
            raise ValueError("CHOICE_FIELD_AMBIGUOUS")
        seen.add(key)
        choices = []
        records = set()
        for c in f.candidates:
            key = (c.target_type, c.record_id)
            if key in records or (f.field == "source" and c.target_type != TargetType.own_account):
                raise ValueError("CHOICE_RECORD_INVALID")
            records.add(key)
            choices.append(
                {
                    "choice_id": uuid.uuid4().hex,
                    "record_id": c.record_id,
                    "target_type": c.target_type.value,
                    "label": record_label(conn, user_id, c.target_type, c.record_id),
                }
            )
        prepared = {
            "field_id": uuid.uuid4().hex,
            "leg_id": f.leg_id,
            "field": f.field,
            "prompt": f.prompt,
            "choices": choices,
        }
        if f.field == "source" and f.amount is not None:
            prepared["amount"] = f.amount
        result.append(prepared)
    return result


def _balances(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT a.account_id, a.balance_cents FROM accounts a JOIN account_holders h "
        "ON h.account_id = a.account_id WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["account_id"]: int(r["balance_cents"]) for r in rows}


def _public_choice(c: dict, amount: str | None, balances: dict[str, int]) -> dict:
    """A choice as the customer sees it. The balance is read live and is display only:
    it is never part of the frozen label, so a balance change cannot break the answer."""
    out = {"choice_id": c["choice_id"], "label": c["label"]}
    cents = balances.get(c["record_id"]) if c["target_type"] == TargetType.own_account else None
    if cents is not None:
        out["available"] = f"{Decimal(cents) / 100:.2f}"
        if amount is not None and Decimal(cents) < Decimal(amount) * 100:
            out["insufficient"] = True
    return out


def public_question(
    q: dict, conn: sqlite3.Connection | None = None, user_id: str | None = None
) -> dict:
    balances = _balances(conn, user_id) if conn is not None and user_id else {}
    return {
        "revision": q["revision"],
        "fields": [
            {
                **f,
                "choices": [_public_choice(c, f.get("amount"), balances) for c in f["choices"]],
            }
            for f in q["fields"]
        ],
    }


def save_question(conn: sqlite3.Connection, conversation_id: str, user_id: str, q: dict) -> None:
    if len(canonical_bytes(q)) > 24000:
        raise ValueError("QUESTION_CONTEXT_TOO_LARGE")
    append_audit(
        conn,
        source="gateway",
        conversation_id=conversation_id,
        user_id=user_id,
        step="gateway.clarification.question",
        payload=q,
    )


def check_question(
    conn: sqlite3.Connection, conversation_id: str, turn_id: str, revision: int
) -> dict | None:
    q = question(conn, conversation_id, turn_id)
    latest = conn.execute(
        "SELECT turn_id FROM conversation_turns WHERE conversation_id = ? "
        "AND role = 'app_question' ORDER BY seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if q and (q["revision"] != revision or latest["turn_id"] != turn_id):
        raise ValueError("STALE_QUESTION")
    return q


def selected_answer(
    conn: sqlite3.Connection,
    user_id: str,
    q: dict,
    revision: int | None,
    selections: list[Selection],
) -> tuple[str, list[dict]]:
    if revision != q["revision"]:
        raise ValueError("STALE_QUESTION_REVISION")
    fields = {f["field_id"]: f for f in q["fields"]}
    if not selections or len(selections) != len(fields):
        raise ValueError("CHOICE_ANSWER_AMBIGUOUS")
    chosen = []
    seen = set()
    # Read as the customer's own answer: "John Tan (account ending 8892), from current
    # (account ending 5678)", with "Payment 2: …" only when several payments are answered.
    # Each label stays verbatim, so its span is exact selection evidence.
    several = len({f["leg_id"] for f in q["fields"]}) > 1
    text = ""
    leg = None
    for s in selections:
        f = fields.get(s.field_id)
        if f is None or s.field_id in seen or (s.leg_id, s.field) != (f["leg_id"], f["field"]):
            raise ValueError("CHOICE_BINDING_INVALID")
        seen.add(s.field_id)
        c = next((c for c in f["choices"] if c["choice_id"] == s.choice_id), None)
        if c is None:
            raise ValueError("CHOICE_ID_INVALID")
        if sum(other["label"] == c["label"] for other in f["choices"]) != 1:
            raise ValueError("CHOICE_ANSWER_AMBIGUOUS")
        label = record_label(conn, user_id, TargetType(c["target_type"]), c["record_id"])
        if label != c["label"]:
            raise ValueError("CHOICE_RECORD_CHANGED")
        if f["leg_id"] != leg:
            text += "; " if text else ""
            text += f"Payment {f['leg_id'].removeprefix('leg-')}: " if several else ""
            leg = f["leg_id"]
        else:
            text += ", "
        text += "from " if f["field"] == "source" else ""
        start = len(text)
        text += label
        chosen.append(
            {
                **s.model_dump(),
                "record_id": c["record_id"],
                "target_type": c["target_type"],
                "span": {"start": start, "end": len(text), "text": label},
            }
        )
    return text, chosen


def last_question(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    """The latest question's turn ID and its saved (untrusted) extraction, if any."""
    row = conn.execute(
        "SELECT turn_id FROM conversation_turns WHERE conversation_id = ? "
        "AND role = 'app_question' ORDER BY seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    q = question(conn, conversation_id, row["turn_id"])
    if q is None or q.get("extraction") is None:
        return None
    return {"turn_id": row["turn_id"], "extraction": q["extraction"]}


def structured_update(conn: sqlite3.Connection, conversation_id: str, revision: int) -> dict | None:
    answers = events(conn, conversation_id, "gateway.clarification.answer")
    if not answers or answers[-1]["revision"] != revision:
        return None
    answer = answers[-1]
    q = question(conn, conversation_id, answer["question_id"])
    if q is None or q.get("extraction") is None:
        return None
    return {"extraction": q["extraction"], "selections": answer["selections"]}
