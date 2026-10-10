"""Current conversation turns and deterministic location of quoted words.

The model only returns words it copied. This module finds where those words sit in a
current user turn, so the cited span (turn, start, end, text) is computed by trusted
code and passes the gateway's location check. Replaced transcript originals are never
visible or citable, and app questions are never citable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "LocateError",
    "QUESTION_ROLE",
    "Span",
    "USER_ROLES",
    "VisibleTurn",
    "find_spans",
    "locate",
    "quoted_regions",
    "visible_turns",
]

USER_ROLES = frozenset({"user_text", "user_asr_final", "user_transcript_edit", "user_answer"})
QUESTION_ROLE = "app_question"

_QUOTED_RE = re.compile(r"\"[^\"\n]{1,300}\"|“[^”\n]{1,300}”")


@dataclass(frozen=True)
class VisibleTurn:
    turn_id: str
    role: str
    text: str

    @property
    def is_user(self) -> bool:
        return self.role in USER_ROLES


@dataclass(frozen=True)
class Span:
    turn_id: str
    start: int
    end: int
    text: str

    def overlaps(self, other: Span) -> bool:
        return self.turn_id == other.turn_id and self.start < other.end and other.start < self.end

    def evidence(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id, "start": self.start, "end": self.end, "text": self.text}


class LocateError(StrEnum):
    NOT_FOUND = "QUOTE_NOT_FOUND"
    IN_REFERENCE = "QUOTE_IN_REFERENCE"


def visible_turns(raw_turns: Iterable[dict[str, Any]]) -> list[VisibleTurn]:
    """Ordered current turns: each edited turn is shown once, as its latest correction,
    at the original's position. Replaced originals are dropped."""
    turns = sorted(raw_turns, key=lambda t: t["seq"])
    by_id = {t["turn_id"]: t for t in turns}
    latest_edit: dict[str, dict[str, Any]] = {}
    for turn in turns:
        if turn.get("edits_turn_id"):
            latest_edit[turn["edits_turn_id"]] = turn

    out: list[VisibleTurn] = []
    for turn in turns:
        if turn.get("edits_turn_id") in by_id:
            continue  # shown at the position of the turn it corrects
        final, seen = turn, set()
        while final["turn_id"] in latest_edit and final["turn_id"] not in seen:
            seen.add(final["turn_id"])
            final = latest_edit[final["turn_id"]]
        if final["role"] in USER_ROLES or final["role"] == QUESTION_ROLE:
            out.append(VisibleTurn(final["turn_id"], final["role"], final["text"]))
    return out


def find_spans(quote: str, turns: Sequence[VisibleTurn]) -> list[Span]:
    """Every whole-word occurrence of *quote* in a user turn (case-insensitive)."""
    needle = quote.strip()
    if not needle:
        return []
    folded_needle = needle.casefold()
    out: list[Span] = []
    for turn in turns:
        if not turn.is_user:
            continue
        text = turn.text
        folded = text.casefold()
        if len(folded) != len(text) or len(folded_needle) != len(needle):
            folded, key = text, needle  # length-changing case folds: exact match only
        else:
            key = folded_needle
        pos = folded.find(key)
        while pos != -1:
            end = pos + len(key)
            if _word_bounded(text, pos, end):
                out.append(Span(turn.turn_id, pos, end, text[pos:end]))
            pos = folded.find(key, pos + 1)
    return out


def locate(
    quote: str,
    turns: Sequence[VisibleTurn],
    *,
    anchor: Span | None = None,
    used: Iterable[Span] = (),
    blocked: Iterable[Span] = (),
) -> Span | LocateError:
    """Pick one occurrence of *quote* to cite.

    Occurrences inside *blocked* regions (references, quoted text) are never used.
    Preference: not already cited for the same field, in the anchor's turn and nearest
    to it, then the latest turn, then exact capitalisation, then leftmost.
    """
    candidates = find_spans(quote, turns)
    if not candidates:
        return LocateError.NOT_FOUND
    blocked = list(blocked)
    allowed = [c for c in candidates if not any(c.overlaps(b) for b in blocked)]
    if not allowed:
        return LocateError.IN_REFERENCE
    used_keys = {(s.turn_id, s.start) for s in used}
    order = {t.turn_id: i for i, t in enumerate(turns)}
    needle = quote.strip()

    def key(span: Span) -> tuple[int, int, int, int, int]:
        in_anchor = anchor is not None and span.turn_id == anchor.turn_id
        distance = abs(span.start - anchor.start) if in_anchor and anchor else span.start
        return (
            (span.turn_id, span.start) in used_keys,
            not in_anchor,
            -order[span.turn_id],
            span.text != needle,
            distance,
        )

    return min(allowed, key=key)


def quoted_regions(turns: Sequence[VisibleTurn]) -> list[Span]:
    """Text the user put in double quotes. Treated like a reference: never a source of
    amounts, recipients or accounts, whatever the model says."""
    return [
        Span(turn.turn_id, m.start(), m.end(), m.group(0))
        for turn in turns
        if turn.is_user
        for m in _QUOTED_RE.finditer(turn.text)
    ]


def _word_bounded(text: str, start: int, end: int) -> bool:
    if text[start].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    return not (text[end - 1].isalnum() and end < len(text) and text[end].isalnum())
