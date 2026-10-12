"""Gateway-owned bank data access layer.

All SQL queries live here.  Only gateway code imports this module.
The agent and validator use HTTP clients (``shared/clients/bank_read.py``).

Money is stored as INTEGER minor units (cents) in SQLite and converted
to/from :class:`~shared.money.Money` at the boundary.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.payees import PayeeNames, payee_names
from ledger.schema import cents_to_money, connect, init_db, money_to_cents
from ledger.seed import seed_db
from shared.money import Money
from shared.names import fuzzy_mentions, mentions
from shared.timeutil import to_iso

__all__ = [
    "BankStore",
    "QUOTE_EXPIRY_MINUTES",
    "MOCK_PRICES",
]

#: Quote expiry duration in minutes.
QUOTE_EXPIRY_MINUTES = 5

#: Fixed mock SGX basis prices per symbol (two-decimal SGD strings).
MOCK_PRICES: dict[str, str] = {
    "D05": "38.50",
    "O39": "9.55",
    "U11": "28.30",
    "Z74": "12.90",
}

#: Maximum list size for history and other list endpoints.
MAX_LIST_SIZE = 50

#: Maximum query length for payee search.
MAX_QUERY_LENGTH = 200


class BankStore:
    """Read/query access to the mock bank ledger.

    This class is instantiated by gateway code with a DB path.
    Financial writes happen only in ``gateway.execution``.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self._db_path)
        return conn

    def init_and_seed(self) -> None:
        """Initialize the schema and seed synthetic data."""
        conn = self._conn()
        try:
            init_db(conn)
            seed_db(conn)
        finally:
            conn.close()

    # ── Accounts ─────────────────────────────────────────────────────

    def get_accounts(self, user_id: str) -> list[dict[str, Any]]:
        """Return all accounts the user holds (owner or joint)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT a.account_id, a.type, a.masked_suffix, a.balance_cents,
                       ah.role, a.created_at
                FROM accounts a
                JOIN account_holders ah ON ah.account_id = a.account_id
                WHERE ah.user_id = ?
                ORDER BY a.account_id
                """,
                (user_id,),
            ).fetchall()
            return [
                {
                    "account_id": row["account_id"],
                    "type": row["type"],
                    "masked_suffix": row["masked_suffix"],
                    "balance": cents_to_money(row["balance_cents"]).to_str(),
                    "role": row["role"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_account(self, user_id: str, account_id: str) -> dict[str, Any] | None:
        """Return a single account if the user holds it, else None."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT a.account_id, a.type, a.masked_suffix, a.balance_cents,
                       ah.role, a.created_at
                FROM accounts a
                JOIN account_holders ah ON ah.account_id = a.account_id
                WHERE ah.user_id = ? AND a.account_id = ?
                """,
                (user_id, account_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "account_id": row["account_id"],
                "type": row["type"],
                "masked_suffix": row["masked_suffix"],
                "balance": cents_to_money(row["balance_cents"]).to_str(),
                "role": row["role"],
                "created_at": row["created_at"],
            }
        finally:
            conn.close()

    # ── Payees ────────────────────────────────────────────────────────

    def search_payees(self, user_id: str, query: str) -> list[dict[str, Any]]:
        """Return ALL matching payee candidates for a name, alias or nickname.

        - ``match: "exact"``: the query is part of the display name (case-insensitive),
          or contains the payee's full name, an alias or a nickname under the shared rule
          (:func:`shared.names.mentions`: case, punctuation and spacing between words
          are ignored).
        - ``match: "fuzzy"``: otherwise, a near spelling of one of those names
          (:func:`shared.names.fuzzy_mentions`). A suggestion only: the agent must ask
          the customer, and the evidence check never accepts it.
        - Never auto-select; never rank-and-pick. Exact candidates come first.
        - ``names`` lists the payee's aliases and nicknames so the caller can match them.
        """
        query = query.strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            return []

        conn = self._conn()
        try:
            names = payee_names(conn, user_id)
            rows = conn.execute(
                """
                SELECT payee_id, display_name, bank, masked_suffix, kind, sanctioned
                FROM payees WHERE owner_user_id = ? ORDER BY payee_id
                """,
                (user_id,),
            ).fetchall()
            exact: list[dict[str, Any]] = []
            fuzzy: list[dict[str, Any]] = []
            needle = query.casefold()
            for row in rows:
                extra = names.get(row["payee_id"], PayeeNames()).all
                full = (row["display_name"], *extra)
                if needle in row["display_name"].casefold() or mentions(query, full):
                    match, bucket = "exact", exact
                elif fuzzy_mentions(query, (*full, *row["display_name"].split())):
                    match, bucket = "fuzzy", fuzzy
                else:
                    continue
                bucket.append(
                    {
                        "payee_id": row["payee_id"],
                        "display_name": row["display_name"],
                        "bank": row["bank"],
                        "masked_suffix": row["masked_suffix"],
                        "kind": row["kind"],
                        "sanctioned": bool(row["sanctioned"]),
                        "names": list(extra),
                        "match": match,
                    }
                )
            return exact + fuzzy
        finally:
            conn.close()

    # ── Billers ──────────────────────────────────────────────────────

    def get_billers(self, user_id: str) -> list[dict[str, Any]]:
        """Return saved billers with bill references for *user_id*."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT b.biller_id, b.display_name, b.category,
                       sb.bill_reference, sb.saved_at
                FROM saved_billers sb
                JOIN billers b ON b.biller_id = sb.biller_id
                WHERE sb.user_id = ?
                ORDER BY b.display_name
                """,
                (user_id,),
            ).fetchall()
            return [
                {
                    "biller_id": row["biller_id"],
                    "display_name": row["display_name"],
                    "category": row["category"],
                    "bill_reference": row["bill_reference"],
                    "saved_at": row["saved_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # ── Cards ────────────────────────────────────────────────────────

    def get_cards(self, user_id: str) -> list[dict[str, Any]]:
        """Return owned cards with outstanding balance."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT card_id, owner_user_id, masked_suffix, outstanding_cents, created_at
                FROM cards
                WHERE owner_user_id = ?
                ORDER BY card_id
                """,
                (user_id,),
            ).fetchall()
            return [
                {
                    "card_id": row["card_id"],
                    "masked_suffix": row["masked_suffix"],
                    "outstanding": cents_to_money(row["outstanding_cents"]).to_str(),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_card(self, user_id: str, card_id: str) -> dict[str, Any] | None:
        """Return a single card if owned by user, else None."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT card_id, owner_user_id, masked_suffix, outstanding_cents, created_at
                FROM cards
                WHERE owner_user_id = ? AND card_id = ?
                """,
                (user_id, card_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "card_id": row["card_id"],
                "masked_suffix": row["masked_suffix"],
                "outstanding": cents_to_money(row["outstanding_cents"]).to_str(),
            }
        finally:
            conn.close()

    # ── Holdings ─────────────────────────────────────────────────────

    def get_holdings(self, user_id: str) -> list[dict[str, Any]]:
        """Return equity holdings for *user_id*."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT holding_id, owner_user_id, symbol, quantity, updated_at
                FROM holdings
                WHERE owner_user_id = ?
                ORDER BY symbol
                """,
                (user_id,),
            ).fetchall()
            return [
                {
                    "holding_id": row["holding_id"],
                    "symbol": row["symbol"],
                    "quantity": row["quantity"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # ── Quotes ───────────────────────────────────────────────────────

    def get_quote(self, symbol: str, now: datetime | None = None) -> dict[str, Any]:
        """Create a new quote row with ``expires_at = now + 5 minutes``.

        Fixed mock price per symbol.  Clock is injectable via *now*.
        Quotes are never mutated; expired quotes stay expired.
        """
        symbol = symbol.upper().strip()
        if symbol not in MOCK_PRICES:
            raise ValueError(f"unsupported symbol: {symbol!r}")

        if now is None:
            now = datetime.now(tz=UTC)

        quoted_at = to_iso(now)
        expires_at_dt = now + timedelta(minutes=QUOTE_EXPIRY_MINUTES)
        expires_at = to_iso(expires_at_dt)

        quote_id = f"quote-{symbol}-{uuid.uuid4().hex[:12]}"
        unit_price = Money(MOCK_PRICES[symbol])
        unit_price_cents = money_to_cents(unit_price)

        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO equity_quotes
                    (quote_id, symbol, unit_price_cents, quoted_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (quote_id, symbol, unit_price_cents, quoted_at, expires_at),
            )
            conn.commit()
            return {
                "quote_id": quote_id,
                "symbol": symbol,
                "unit_price": unit_price.to_str(),
                "quoted_at": quoted_at,
                "expires_at": expires_at,
            }
        finally:
            conn.close()

    def fetch_quote(self, quote_id: str) -> dict[str, Any] | None:
        """Fetch a quote by ID.  Returns None if not found."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT quote_id, symbol, unit_price_cents, quoted_at, expires_at
                FROM equity_quotes
                WHERE quote_id = ?
                """,
                (quote_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "quote_id": row["quote_id"],
                "symbol": row["symbol"],
                "unit_price": cents_to_money(row["unit_price_cents"]).to_str(),
                "quoted_at": row["quoted_at"],
                "expires_at": row["expires_at"],
            }
        finally:
            conn.close()

    # ── Transactions (read only; written by gateway.execution) ───────

    def get_transactions(self, user_id: str, limit: int = MAX_LIST_SIZE) -> list[dict[str, Any]]:
        """Return recent transaction history for *user_id*.

        ``direction`` is ``debit`` for a leg's outgoing amount and ``credit`` for
        the destination side of an own-account transfer.
        """
        limit = min(limit, MAX_LIST_SIZE)
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT transaction_id, user_id, account_id, action, target_type,
                       target_id, amount_cents, status, created_at, direction
                FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC, transaction_id
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [
                {
                    "transaction_id": row["transaction_id"],
                    "account_id": row["account_id"],
                    "action": row["action"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "amount": cents_to_money(row["amount_cents"]).to_str(),
                    "direction": row["direction"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()
