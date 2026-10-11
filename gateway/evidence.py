"""Verify the agent's cited evidence and build trusted lineage for a draft leg.

The agent proposes, for each leg, the exact words in a user turn that support the
amount, the recipient (target) and the source account. The agent's claims are
untrusted; this module checks them deterministically against gateway-held data:

- the cited turn belongs to the conversation, is user-originated (never an
  ``app_question``) and has not been replaced by a later transcript edit;
- ``validate_span`` proves the quoted words sit at that location (location only), and
  the span must be whole words: "Mum" cut out of "Mumbai" is not a citation;
- the target and source words must match the bank's own name, alias, account type
  or masked suffix for exactly one of the user's records of that kind, so "John"
  alone cannot select John Doe when John Tan also exists;
- an amount whose words fall inside the leg's free-text reference is rejected, so
  a bill reference such as "INV 900" can never become the amount.

Amount words are parsed with the shared deterministic parser (``shared/amounts.py``)
and must mean exactly the proposed amount, fraction or "the rest".
Lineage records are built here, by trusted code, never copied from the proposal.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ledger.schema import money_to_cents
from shared.amounts import (
    FractionAmount,
    LiteralAmount,
    OutstandingAmount,
    RemainingAmount,
    parse_amount,
)
from shared.lineage import (
    build_bank_record_ref,
    build_user_selection_ref,
    build_user_turn_ref,
    validate_critical_field_lineage,
    validate_span,
)
from shared.money import Money
from shared.schemas import Lineage, SourceRef, TargetType

__all__ = ["EvidenceError", "TurnIndex", "Span", "build_leg_lineage"]

USER_ROLES = frozenset({"user_text", "user_asr_final", "user_transcript_edit", "user_answer"})

#: Display names for the mock SGX symbols, so "DBS" can cite D05.
SGX_NAMES: dict[str, tuple[str, ...]] = {
    "D05": ("dbs",),
    "O39": ("ocbc",),
    "U11": ("uob",),
    "Z74": ("singtel",),
}


class EvidenceError(ValueError):
    """The cited evidence does not support the proposed value. Message is a stable code."""


@dataclass(frozen=True)
class Span:
    turn_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    role: str
    text: str


class TurnIndex:
    """Gateway-held turns of one conversation, with superseded turns excluded."""

    def __init__(self, conn: sqlite3.Connection, conversation_id: str) -> None:
        rows = conn.execute(
            "SELECT turn_id, role, text, edits_turn_id FROM conversation_turns "
            "WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        edited = {r["edits_turn_id"] for r in rows if r["edits_turn_id"]}
        self._turns = {
            r["turn_id"]: _Turn(r["turn_id"], r["role"], r["text"])
            for r in rows
            if r["turn_id"] not in edited
        }

    def cite(self, span: Span) -> SourceRef:
        """Return a user-turn SourceRef for a verified citation, else raise."""
        turn = self._turns.get(span.turn_id)
        if turn is None or turn.role not in USER_ROLES:
            raise EvidenceError("EVIDENCE_TURN_INVALID")
        if not span.text.strip() or not validate_span(turn.text, span.start, span.end, span.text):
            raise EvidenceError("EVIDENCE_SPAN_MISMATCH")
        if not _whole_words(turn.text, span.start, span.end):
            # A fragment of a longer word ("Mum" in "Mumbai", "900" in "INV9001") is not
            # what the user said. Same rule as the agent's own citations (T-13 finding).
            raise EvidenceError("EVIDENCE_SPAN_MISMATCH")
        if turn.role == "user_answer":
            return build_user_selection_ref(turn.turn_id, span.start, span.end)
        return build_user_turn_ref(turn.turn_id, span.start, span.end)

    def turn_text(self, turn_id: str) -> str:
        return self._turns[turn_id].text

    def user_said(self, phrase: str) -> bool:
        """True if *phrase* appears as whole words in a current user-originated turn."""
        needle = phrase.casefold()
        for turn in self._turns.values():
            if turn.role not in USER_ROLES:
                continue
            text = turn.text.casefold()
            pos = text.find(needle)
            while pos != -1:
                if _whole_words(turn.text, pos, pos + len(needle)):
                    return True
                pos = text.find(needle, pos + 1)
        return False


def _whole_words(text: str, start: int, end: int) -> bool:
    """The span neither starts nor ends inside a word (``agent.turns._word_bounded``)."""
    if text[start].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    return not (text[end - 1].isalnum() and end < len(text) and text[end].isalnum())


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _mentions(span_text: str, names: tuple[str, ...]) -> bool:
    """True if any name appears as a whole word or phrase in the span."""
    haystack = f" {_normalize(span_text)} "
    return any(name and f" {_normalize(name)} " in haystack for name in names)


Names = tuple[tuple[str, ...], tuple[str, ...]]


def _candidates(
    conn: sqlite3.Connection, user_id: str, target_type: TargetType
) -> dict[str, Names]:
    """Every record of this kind the user owns, mapped to (strong, weak) identifying names.

    Strong names identify one record on their own: a full display name, an alias, a
    masked suffix, an account type, a symbol. Weak names are single words of a display
    name ("John"), which may be shared by several records.
    """
    if target_type == TargetType.own_account:
        rows = conn.execute(
            "SELECT a.account_id, a.type, a.masked_suffix FROM accounts a "
            "JOIN account_holders h ON h.account_id = a.account_id WHERE h.user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["account_id"]: ((r["type"], r["masked_suffix"]), ()) for r in rows}
    if target_type in (TargetType.saved_payee, TargetType.saved_paynow):
        kind = "paynow" if target_type == TargetType.saved_paynow else "bank"
        rows = conn.execute(
            "SELECT payee_id, display_name, masked_suffix FROM payees "
            "WHERE owner_user_id = ? AND kind = ?",
            (user_id, kind),
        ).fetchall()
        aliases: dict[str, list[str]] = {}
        for a in conn.execute(
            "SELECT alias, payee_id FROM payee_aliases WHERE owner_user_id = ?", (user_id,)
        ):
            aliases.setdefault(a["payee_id"], []).append(a["alias"])
        return {
            r["payee_id"]: (
                (
                    r["display_name"],
                    *((r["masked_suffix"],) if r["masked_suffix"] else ()),
                    *aliases.get(r["payee_id"], []),
                ),
                tuple(r["display_name"].split()),
            )
            for r in rows
        }
    if target_type == TargetType.biller:
        rows = conn.execute(
            "SELECT b.biller_id, b.display_name FROM saved_billers sb "
            "JOIN billers b ON b.biller_id = sb.biller_id WHERE sb.user_id = ?",
            (user_id,),
        ).fetchall()
        return {
            r["biller_id"]: ((r["display_name"],), (r["display_name"].split()[0],)) for r in rows
        }
    if target_type == TargetType.owned_card:
        rows = conn.execute(
            "SELECT card_id, masked_suffix FROM cards WHERE owner_user_id = ?", (user_id,)
        ).fetchall()
        return {r["card_id"]: (("card", "credit card", r["masked_suffix"]), ()) for r in rows}
    if target_type == TargetType.sgx_symbol:
        return {sym: ((sym, *names), ()) for sym, names in SGX_NAMES.items()}
    raise EvidenceError("UNKNOWN_TARGET_TYPE")


def _identify(
    conn: sqlite3.Connection,
    turns: TurnIndex,
    user_id: str,
    target_type: TargetType,
    record_id: str,
    span: Span,
    code: str,
) -> list[SourceRef]:
    """The cited words must name this record and no other record of the same kind."""
    user_ref = turns.cite(span)
    candidates = _candidates(conn, user_id, target_type)
    # A full name, alias or suffix ("John Doe", "Mum", "4521") identifies a record even
    # if one of its words ("John") is shared; a shared word alone does not.
    matches = [rid for rid, (strong, _) in candidates.items() if _mentions(span.text, strong)]
    if not matches:
        matches = [rid for rid, (_, weak) in candidates.items() if _mentions(span.text, weak)]
    if matches != [record_id]:
        raise EvidenceError(code if record_id not in matches else f"{code}_AMBIGUOUS")
    return [user_ref, build_bank_record_ref(record_id)]


def _check_amount_words(
    conn: sqlite3.Connection, user_id: str, leg: dict[str, Any], words: str
) -> None:
    """The cited amount words must mean the proposed amount (shared deterministic parser).

    Literal: the words parse to exactly the proposed amount, or say "card balance" for a
    card payment whose amount equals the bank's outstanding balance. Relative: the words
    parse to the same fraction or to "the rest". Parsing is the same code the agent uses,
    so a model cannot cite "fifteen" for 50.00.
    """
    parsed = parse_amount(words)
    expr = leg.get("amount_expr") or {"kind": "literal", "amount": leg.get("amount")}
    kind = expr.get("kind")
    if kind == "literal":
        try:
            proposed = Money(expr.get("amount"))
        except (TypeError, ValueError):
            raise EvidenceError("AMOUNT_EXPR_INVALID") from None
        if isinstance(parsed, LiteralAmount) and parsed.amount == proposed:
            return
        if isinstance(parsed, OutstandingAmount) and leg.get("action") == "pay_card":
            row = conn.execute(
                "SELECT outstanding_cents FROM cards WHERE card_id = ? AND owner_user_id = ?",
                (leg.get("target_id"), user_id),
            ).fetchone()
            if row is not None and money_to_cents(proposed) == int(row["outstanding_cents"]):
                return
    elif kind == "fraction_of_leg" and isinstance(parsed, FractionAmount):
        if (
            parsed.numerator * int(expr.get("denominator", 0))
            == int(expr.get("numerator", 0)) * parsed.denominator
        ):
            return
    elif kind == "remaining_balance" and isinstance(parsed, RemainingAmount):
        return
    raise EvidenceError("AMOUNT_EVIDENCE_MISMATCH")


def build_leg_lineage(
    conn: sqlite3.Connection,
    turns: TurnIndex,
    user_id: str,
    leg: dict[str, Any],
    *,
    relative_parents: list[SourceRef] | None = None,
) -> Lineage:
    """Verify one leg's evidence and return trusted lineage for its critical fields.

    ``leg["evidence"]`` must cite ``amount``, ``target`` and ``source`` spans.
    For a relative amount ("half of that"), ``relative_parents`` carries the
    lineage of the referenced leg's amount; taint is inherited through it.
    """
    evidence = leg.get("evidence") or {}
    try:
        spans = {key: Span(**evidence[key]) for key in ("amount", "target", "source")}
    except (KeyError, TypeError):
        raise EvidenceError("EVIDENCE_MISSING") from None

    amount_ref = turns.cite(spans["amount"])
    reference = (leg.get("reference") or "").strip()
    if reference:
        turn_text = turns.turn_text(spans["amount"].turn_id)
        amount = spans["amount"]
        overlaps_reference = any(
            amount.start < m.end() and m.start() < amount.end
            for m in re.finditer(re.escape(reference), turn_text, flags=re.IGNORECASE)
        )
        if overlaps_reference or _normalize(amount.text) in _normalize(reference):
            raise EvidenceError("AMOUNT_FROM_REFERENCE")

    _check_amount_words(conn, user_id, leg, spans["amount"].text)

    target_refs = _identify(
        conn,
        turns,
        user_id,
        TargetType(leg["target_type"]),
        leg["target_id"],
        spans["target"],
        "TARGET_EVIDENCE",
    )
    source_refs = _identify(
        conn,
        turns,
        user_id,
        TargetType.own_account,
        leg["source_account_id"],
        spans["source"],
        "SOURCE_EVIDENCE",
    )

    if relative_parents is None:
        lineage = Lineage(source_refs=[amount_ref, *target_refs, *source_refs])
    else:
        lineage = Lineage(
            source_refs=[SourceRef(kind="derived"), *target_refs, *source_refs],
            parent_refs=[amount_ref, *relative_parents],
        )
    for field in ("amount", "target_id", "source_account_id"):
        validate_critical_field_lineage(field, lineage)
    return lineage
