"""Saved-payee names: bank aliases, customer nicknames, and nickname management.

A payee is named by its display name, its masked account suffix, its **aliases** (set by
the bank, ``payee_aliases``) and its **nicknames** (added by the customer on the Payees
page, ``payee_nicknames``). Aliases and nicknames are both exact names: the evidence
check, the validator's bank labels, payee search and ASR hotwords all read them through
:func:`payee_names`.

A nickname is refused if it would name any other record the customer could pay or pay
from (another payee, an own account, a saved biller, the card or an SGX stock), so a new
nickname can never make an existing instruction ambiguous or redirect it. Only the
signed-in customer can add or remove one; the agent has no route to this module's writes.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from gateway.audit import append_audit
from ledger.schema import connect
from shared.names import FILLER_WORDS, compact, mentions, normalize
from shared.timeutil import now_utc, to_iso

__all__ = [
    "MAX_NICKNAMES_PER_PAYEE",
    "MAX_NICKNAME_LEN",
    "NicknameError",
    "PayeeNames",
    "add_nickname",
    "list_payees",
    "payee_names",
    "remove_nickname",
]

MAX_NICKNAME_LEN = 30
MAX_NICKNAMES_PER_PAYEE = 10
_NICKNAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-.]*$")
#: Words that already mean something else in a payment instruction.
_RESERVED = frozenset(
    {"me", "myself", "savings", "saving", "current", "joint", "card", "credit", "bill"}
)


@dataclass
class PayeeNames:
    aliases: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return [*self.aliases, *self.nicknames]


class NicknameError(Exception):
    """A refused nickname change; ``code`` is a stable reason code, ``status`` the HTTP status."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def payee_names(conn: sqlite3.Connection, user_id: str) -> dict[str, PayeeNames]:
    """Aliases and nicknames for every payee the user owns, keyed by payee ID (sorted)."""
    out: dict[str, PayeeNames] = {}
    for r in conn.execute(
        "SELECT alias, payee_id FROM payee_aliases WHERE owner_user_id = ? ORDER BY alias",
        (user_id,),
    ):
        out.setdefault(r["payee_id"], PayeeNames()).aliases.append(r["alias"])
    for r in conn.execute(
        "SELECT nickname, payee_id FROM payee_nicknames WHERE owner_user_id = ? "
        "ORDER BY created_at, nickname",
        (user_id,),
    ):
        out.setdefault(r["payee_id"], PayeeNames()).nicknames.append(r["nickname"])
    return out


def list_payees(db_path: str, user_id: str) -> list[dict[str, object]]:
    """The user's saved payees with their aliases and nicknames, for the Payees page."""
    conn = connect(db_path)
    try:
        return _list(conn, user_id)
    finally:
        conn.close()


def _list(conn: sqlite3.Connection, user_id: str) -> list[dict[str, object]]:
    names = payee_names(conn, user_id)
    rows = conn.execute(
        "SELECT payee_id, display_name, bank, masked_suffix, kind FROM payees "
        "WHERE owner_user_id = ? ORDER BY display_name, payee_id",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        n = names.get(r["payee_id"], PayeeNames())
        seen = {normalize(r["display_name"])}
        aliases = []
        for a in n.aliases:  # the seed stores "Mum" and "mum"; show each name once
            if normalize(a) not in seen:
                seen.add(normalize(a))
                aliases.append(a)
        out.append(
            {
                "payee_id": r["payee_id"],
                "display_name": r["display_name"],
                "bank": r["bank"],
                "masked_suffix": r["masked_suffix"],
                "kind": r["kind"],
                "aliases": aliases,
                "nicknames": n.nicknames,
            }
        )
    return out


def _clean(nickname: str) -> str:
    text = " ".join(nickname.split())
    if not text or len(text) > MAX_NICKNAME_LEN or not _NICKNAME_RE.match(text):
        raise NicknameError("NICKNAME_INVALID", 422)
    key_words = normalize(text).split()
    if not key_words or all(w in FILLER_WORDS or w in _RESERVED for w in key_words):
        raise NicknameError("NICKNAME_RESERVED", 422)
    if len(compact(text)) < 2:
        raise NicknameError("NICKNAME_INVALID", 422)
    return text


def _other_record_names(
    conn: sqlite3.Connection, user_id: str, payee_id: str
) -> list[tuple[str, ...]]:
    """Every name of every other record the user can pay or pay from."""
    from gateway.evidence import SGX_NAMES  # imported here: evidence imports this module

    groups: list[tuple[str, ...]] = []
    names = payee_names(conn, user_id)
    for r in conn.execute(
        "SELECT payee_id, display_name, masked_suffix FROM payees WHERE owner_user_id = ?",
        (user_id,),
    ):
        if r["payee_id"] == payee_id:
            continue
        n = names.get(r["payee_id"], PayeeNames())
        groups.append((r["display_name"], *r["display_name"].split(), *n.all))
    for r in conn.execute(
        "SELECT a.type, a.masked_suffix FROM accounts a JOIN account_holders h "
        "ON h.account_id = a.account_id WHERE h.user_id = ?",
        (user_id,),
    ):
        groups.append((r["type"],))
    for r in conn.execute(
        "SELECT b.display_name FROM saved_billers sb JOIN billers b "
        "ON b.biller_id = sb.biller_id WHERE sb.user_id = ?",
        (user_id,),
    ):
        groups.append((r["display_name"], r["display_name"].split()[0]))
    groups.append(("card", "credit card"))
    groups += [(symbol, *aka) for symbol, aka in SGX_NAMES.items()]
    return groups


def add_nickname(db_path: str, user_id: str, payee_id: str, nickname: str) -> dict[str, object]:
    """Add a nickname to one of the user's payees and return the updated payee."""
    text = _clean(nickname)
    key = compact(text)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            payee = conn.execute(
                "SELECT display_name FROM payees WHERE payee_id = ? AND owner_user_id = ?",
                (payee_id, user_id),
            ).fetchone()
            if payee is None:
                raise NicknameError("PAYEE_NOT_FOUND", 404)
            own = payee_names(conn, user_id).get(payee_id, PayeeNames())
            if key in {compact(n) for n in (payee["display_name"], *own.all)}:
                raise NicknameError("NICKNAME_EXISTS", 409)
            if len(own.nicknames) >= MAX_NICKNAMES_PER_PAYEE:
                raise NicknameError("NICKNAME_LIMIT", 422)
            # Clash in either direction: the nickname names another record ("John" while
            # John Tan exists), or another record's name sits inside it ("Mum Lim").
            for group in _other_record_names(conn, user_id, payee_id):
                if mentions(text, group) or any(mentions(g, (text,)) for g in group):
                    raise NicknameError("NICKNAME_CONFLICT", 409)
            taken = conn.execute(
                "SELECT 1 FROM payee_nicknames WHERE owner_user_id = ? AND name_key = ?",
                (user_id, key),
            ).fetchone()
            if taken is not None:
                raise NicknameError("NICKNAME_CONFLICT", 409)
            conn.execute(
                "INSERT INTO payee_nicknames "
                "(owner_user_id, name_key, nickname, payee_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, key, text, payee_id, to_iso(now_utc())),
            )
            append_audit(
                conn,
                source="gateway",
                step="gateway.payee_nickname_added",
                payload={"payee_id": payee_id, "nickname": text},
                user_id=user_id,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return next(p for p in _list(conn, user_id) if p["payee_id"] == payee_id)
    finally:
        conn.close()


def remove_nickname(db_path: str, user_id: str, payee_id: str, nickname: str) -> dict[str, object]:
    """Remove one of the user's nicknames (matched ignoring case and spacing)."""
    key = compact(nickname)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT nickname FROM payee_nicknames "
                "WHERE owner_user_id = ? AND payee_id = ? AND name_key = ?",
                (user_id, payee_id, key),
            ).fetchone()
            if row is None:
                raise NicknameError("NICKNAME_NOT_FOUND", 404)
            conn.execute(
                "DELETE FROM payee_nicknames WHERE owner_user_id = ? AND name_key = ?",
                (user_id, key),
            )
            append_audit(
                conn,
                source="gateway",
                step="gateway.payee_nickname_removed",
                payload={"payee_id": payee_id, "nickname": row["nickname"]},
                user_id=user_id,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return next(p for p in _list(conn, user_id) if p["payee_id"] == payee_id)
    finally:
        conn.close()
