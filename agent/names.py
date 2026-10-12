"""Match the user's words to bank records, and narrow a citation to words that name one record.

Two different questions are answered here:

1. **Resolution** (what did the user mean?). Strong names (full payee name, alias, masked
   suffix, account type, stock code; for payees also bank aliases and the customer's
   nicknames) beat weak ones (one word of a payee's name, "card"). "John Doe" therefore
   means John Doe even though "John" alone matches two payees. More than one remaining
   candidate, or none, means the agent asks; with none, near spellings ("mom" ~ "Mum")
   become a "Did you mean …?" question, never a choice.
2. **Citation** (which words prove it?). The gateway accepts a target/source citation only
   if the cited words name exactly one record of that kind under a flat rule (any name,
   any word; :mod:`shared.names`). :func:`narrow_citation` applies that rule and, if the
   whole quote names several records (e.g. "John Doe" also contains "John"), picks the
   longest sub-span of the user's words that names only the resolved record (e.g. "Doe"
   or "4521").

The gateway remains authoritative; if this mirror disagrees, the gateway rejects the
proposal and the agent turns the rejection into a question.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.turns import Span
from shared.names import fuzzy_mentions
from shared.names import mentions as shared_mentions

__all__ = [
    "Record",
    "RecordKind",
    "SGX_NAMES",
    "account_record",
    "biller_record",
    "card_record",
    "match_records",
    "mentions",
    "narrow_citation",
    "payee_queries",
    "payee_record",
    "sgx_records",
    "suggest_records",
]

RecordKind = Literal["own_account", "bank_payee", "paynow_payee", "biller", "card", "sgx"]

#: Mock SGX symbols and the company names users say for them (same table as the gateway).
SGX_NAMES: dict[str, tuple[str, ...]] = {
    "D05": ("dbs",),
    "O39": ("ocbc",),
    "U11": ("uob",),
    "Z74": ("singtel",),
}

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
_MAX_NARROW_TOKENS = 12
_QUERY_STOPWORDS = frozenset(
    {
        "my",
        "to",
        "the",
        "a",
        "an",
        "and",
        "for",
        "from",
        "account",
        "acct",
        "ending",
        "in",
        "with",
        "of",
        "s",
        "bank",
        "paynow",
        "his",
        "her",
        "their",
        "our",
        "pay",
        "send",
        "transfer",
    }
)


@dataclass(frozen=True)
class Record:
    kind: RecordKind
    record_id: str
    label: str
    strong: tuple[str, ...]
    weak: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def names(self) -> tuple[str, ...]:
        """Every name the gateway accepts for this record (flat, no tiers)."""
        return self.strong + self.weak


def mentions(text: str, names: Iterable[str]) -> bool:
    """True if any name appears in *text* as whole words (the gateway's rule, shared)."""
    return shared_mentions(text, names)


def match_records(text: str, records: Iterable[Record]) -> list[Record]:
    """Records the user's words refer to, strong names first. Never picks among several."""
    records = list(records)
    strong = [r for r in records if mentions(text, r.strong)]
    if strong:
        return strong
    return [r for r in records if mentions(text, r.weak)]


def suggest_records(text: str, records: Iterable[Record]) -> list[Record]:
    """Records whose names are a near spelling of the user's words ("mom" ~ "Mum").

    Only for a "Did you mean …?" question: the gateway never accepts a near spelling as
    evidence, so the customer's answer must name the record exactly.
    """
    return [r for r in records if fuzzy_mentions(text, r.names)]


def narrow_citation(span: Span, record: Record, peers: Sequence[Record]) -> Span | None:
    """The longest part of *span* whose words name *record* and no other peer."""

    def names_only_record(text: str) -> bool:
        hits = [p.record_id for p in peers if mentions(text, p.names)]
        return hits == [record.record_id]

    if names_only_record(span.text):
        return span
    tokens = list(_TOKEN_RE.finditer(span.text))[:_MAX_NARROW_TOKENS]
    windows = sorted(
        ((i, j) for i in range(len(tokens)) for j in range(i, len(tokens))),
        key=lambda w: (w[0] - w[1], w[0]),
    )
    for i, j in windows:
        start, end = tokens[i].start(), tokens[j].end()
        text = span.text[start:end]
        if names_only_record(text):
            return Span(span.turn_id, span.start + start, span.start + end, text)
    return None


def payee_queries(quote: str, limit: int = 6) -> list[str]:
    """Search terms for the payee API: the quote itself, then its significant words."""
    core = re.sub(r"^(?:(?:to|my|the|for)\s+)+", "", quote.strip(), flags=re.IGNORECASE)
    core = re.sub(r"['’]s$", "", core).strip()
    terms = [core] if core else []
    terms += [
        w for w in _TOKEN_RE.findall(core) if len(w) >= 2 and w.casefold() not in _QUERY_STOPWORDS
    ]
    out: list[str] = []
    for term in terms:
        if term.casefold() not in {t.casefold() for t in out}:
            out.append(term)
    return out[:limit]


# ── Record builders (from gateway bank-read responses) ───────────────


def account_record(account: dict[str, Any]) -> Record:
    kind, suffix = account["type"], account["masked_suffix"]
    return Record(
        kind="own_account",
        record_id=account["account_id"],
        label=f"your {kind} account ending {suffix}",
        strong=(kind, suffix),
        data=account,
    )


def payee_record(payee: dict[str, Any], aliases: Iterable[str] = ()) -> Record:
    name = payee["display_name"]
    suffix = payee.get("masked_suffix")
    is_paynow = payee.get("kind") == "paynow"
    if is_paynow:
        label = f"{name} (PayNow)"
    elif suffix:
        label = f"{name} (account ending {suffix})"
    else:
        label = name
    return Record(
        kind="paynow_payee" if is_paynow else "bank_payee",
        record_id=payee["payee_id"],
        label=label,
        strong=(name, *((suffix,) if suffix else ()), *aliases),
        weak=tuple(name.split()),
        data=payee,
    )


def biller_record(biller: dict[str, Any]) -> Record:
    name = biller["display_name"]
    return Record(
        kind="biller",
        record_id=biller["biller_id"],
        label=name,
        strong=(name,),
        weak=(name.split()[0],),
        data=biller,
    )


def card_record(card: dict[str, Any]) -> Record:
    suffix = card["masked_suffix"]
    return Record(
        kind="card",
        record_id=card["card_id"],
        label=f"your card ending {suffix}",
        strong=(suffix,),
        weak=("card", "credit card"),
        data=card,
    )


def sgx_records() -> list[Record]:
    return [
        Record(
            kind="sgx",
            record_id=symbol,
            label=f"{names[0].upper()} ({symbol})",
            strong=(symbol, *names),
        )
        for symbol, names in SGX_NAMES.items()
    ]
