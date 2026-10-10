"""Idempotent synthetic seed for the mock bank.

Running this twice produces identical row counts and balances: every
table is cleared before inserting.  All data is synthetic; no real
account numbers, payees, or bill references are used.

Users:
    - ``user-matthew``: KYC verified tier 2, expires 2027-12-31.
    - ``user-ana``: KYC verified tier 1, holder on the joint account.
"""

from __future__ import annotations

import sqlite3

from ledger.schema import init_db, money_to_cents
from shared.money import Money

__all__ = ["seed_db"]

#: Fixed synthetic timestamps so the seed is deterministic.
_SEED_TS = "2026-10-10T00:00:00.000Z"

#: Mock SGX basis prices (two-decimal SGD).
_MOCK_PRICES: dict[str, str] = {
    "D05": "38.50",
    "O39": "9.55",
    "U11": "28.30",
    "Z74": "12.90",
}

#: Allowed actions for the active mandate (all four MVP actions).
_ALL_ACTIONS = "transfer,pay_bill,pay_card,buy_equity"

#: Mandate expiry (synthetic); policy blocks payments after it.
_MANDATE_EXPIRES = "2027-12-31T23:59:59.000Z"


def _truncate_all(conn: sqlite3.Connection) -> None:
    """Delete all rows from every table, in FK-safe order."""
    # Order matters for foreign keys, but PRAGMA foreign_keys=ON with
    # DELETE will cascade if needed.  We delete in reverse dependency.
    for table in [
        "receipts",
        "transactions",
        "mandates",
        "equity_quotes",
        "holdings",
        "cards",
        "saved_billers",
        "billers",
        "payee_aliases",
        "payees",
        "account_holders",
        "accounts",
        "kyc",
        "users",
    ]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def seed_db(conn: sqlite3.Connection) -> None:
    """Seed the database with synthetic data.  Idempotent."""
    init_db(conn)
    _truncate_all(conn)

    now = _SEED_TS  # deterministic seed

    # ── Users ──
    conn.executemany(
        "INSERT INTO users (user_id, display_name, created_at) VALUES (?, ?, ?)",
        [
            ("user-matthew", "Matthew", now),
            ("user-ana", "Ana", now),
        ],
    )

    # ── KYC ──
    conn.executemany(
        "INSERT INTO kyc (user_id, status, tier, expires_at, checked_at) VALUES (?, ?, ?, ?, ?)",
        [
            ("user-matthew", "verified", 2, "2027-12-31T23:59:59.000Z", now),
            ("user-ana", "verified", 1, "2027-12-31T23:59:59.000Z", now),
        ],
    )

    # ── Accounts ──
    # Matthew: savings 12000.00, current 3500.00, joint 2000.00
    # Ana:     savings 8000.00, shares the joint account
    accounts = [
        ("acct-matthew-savings", "savings", "1234", _cents("12000.00"), now),
        ("acct-matthew-current", "current", "5678", _cents("3500.00"), now),
        ("acct-joint-01", "joint", "9012", _cents("2000.00"), now),
        ("acct-ana-savings", "savings", "3456", _cents("8000.00"), now),
    ]
    conn.executemany(
        "INSERT INTO accounts (account_id, type, masked_suffix, balance_cents, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        accounts,
    )

    # ── Account holders ──
    conn.executemany(
        "INSERT INTO account_holders (user_id, account_id, role) VALUES (?, ?, ?)",
        [
            ("user-matthew", "acct-matthew-savings", "owner"),
            ("user-matthew", "acct-matthew-current", "owner"),
            ("user-matthew", "acct-joint-01", "owner"),
            ("user-ana", "acct-ana-savings", "owner"),
            ("user-ana", "acct-joint-01", "joint"),
        ],
    )

    # ── Payees (Matthew) ──
    payees = [
        # Mum (alias "Mum")
        ("payee-mum", "user-matthew", "Mum", "DBS", "9988", "bank", 0, now),
        # Ah Ma (alias "Ah Ma")
        ("payee-ahma", "user-matthew", "Ah Ma", "POSB", "7766", "bank", 0, now),
        # John Doe ending 4521
        ("payee-johndoe", "user-matthew", "John Doe", "DBS", "4521", "bank", 0, now),
        # John Tan ending 8892
        ("payee-johntan", "user-matthew", "John Tan", "OCBC", "8892", "bank", 0, now),
        # Landlord Lim
        ("payee-lim", "user-matthew", "Landlord Lim", "DBS", "3344", "bank", 0, now),
        # Northwind Trading (sanctioned=true, synthetic)
        ("payee-northwind", "user-matthew", "Northwind Trading", "CITI", "1100", "bank", 1, now),
        # Priya (saved PayNow recipient, masked mobile)
        ("payee-priya", "user-matthew", "Priya", None, "**12", "paynow", 0, now),
        # ── Ana's payees ──
        ("payee-ana-dad", "user-ana", "Dad", "DBS", "5544", "bank", 0, now),
        ("payee-ana-sis", "user-ana", "Sis", "POSB", "2211", "paynow", 0, now),
    ]
    conn.executemany(
        "INSERT INTO payees (payee_id, owner_user_id, display_name, bank, "
        "masked_suffix, kind, sanctioned, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        payees,
    )

    # ── Payee aliases ──
    conn.executemany(
        "INSERT INTO payee_aliases (alias, payee_id, owner_user_id) VALUES (?, ?, ?)",
        [
            ("Mum", "payee-mum", "user-matthew"),
            ("Ah Ma", "payee-ahma", "user-matthew"),
            ("mum", "payee-mum", "user-matthew"),  # lowercase alias too
            ("ah ma", "payee-ahma", "user-matthew"),
            ("Dad", "payee-ana-dad", "user-ana"),
            ("Sis", "payee-ana-sis", "user-ana"),
        ],
    )

    # ── Billers ──
    conn.executemany(
        "INSERT INTO billers (biller_id, display_name, category) VALUES (?, ?, ?)",
        [
            ("biller-spgroup", "SP Group", "utilities"),
            ("biller-singtel", "Singtel", "telecom"),
        ],
    )

    # ── Saved billers (Matthew has SP Group saved with a bill reference) ──
    conn.executemany(
        "INSERT INTO saved_billers (user_id, biller_id, bill_reference, saved_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("user-matthew", "biller-spgroup", "SP-REF-2026-001", now),
            ("user-matthew", "biller-singtel", "SG-REF-2026-002", now),
            ("user-ana", "biller-spgroup", "AN-SP-2026-010", now),
        ],
    )

    # ── Cards ──
    conn.executemany(
        "INSERT INTO cards (card_id, owner_user_id, masked_suffix, outstanding_cents, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("card-matthew-01", "user-matthew", "4242", _cents("642.30"), now),
            ("card-ana-01", "user-ana", "8888", _cents("150.00"), now),
        ],
    )

    # ── Equity quotes (basis prices) ──
    quote_rows = [
        (
            f"quote-{symbol}-seed",
            symbol,
            _cents(price),
            now,
            "2026-10-10T00:05:00.000Z",  # expires 5 min after quoted_at
        )
        for symbol, price in _MOCK_PRICES.items()
    ]
    conn.executemany(
        "INSERT INTO equity_quotes (quote_id, symbol, unit_price_cents, quoted_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        quote_rows,
    )

    # ── Holdings (optional; give Matthew a small D05 position) ──
    conn.executemany(
        "INSERT INTO holdings (holding_id, owner_user_id, symbol, quantity, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("hold-matthew-d05", "user-matthew", "D05", 100, now),
            ("hold-ana-u11", "user-ana", "U11", 50, now),
        ],
    )

    # ── Mandates ──
    # Matthew: active mandate v1, per-leg 1000.00, daily 3000.00, all four actions,
    # at most 4 legs, expires with the synthetic KYC record.
    # Ana: active mandate v1, same limits, all four actions.
    conn.executemany(
        "INSERT INTO mandates (mandate_id, user_id, version, status, per_leg_cents, "
        "daily_cents, allowed_actions, effective_at, created_at, expires_at, max_legs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "mandate-matthew-v1",
                "user-matthew",
                1,
                "active",
                _cents("1000.00"),
                _cents("3000.00"),
                _ALL_ACTIONS,
                now,
                now,
                _MANDATE_EXPIRES,
                4,
            ),
            (
                "mandate-ana-v1",
                "user-ana",
                1,
                "active",
                _cents("1000.00"),
                _cents("3000.00"),
                _ALL_ACTIONS,
                now,
                now,
                _MANDATE_EXPIRES,
                4,
            ),
        ],
    )

    conn.commit()


def _cents(money_str: str) -> int:
    """Convert a money string to integer cents."""
    return money_to_cents(Money(money_str))
