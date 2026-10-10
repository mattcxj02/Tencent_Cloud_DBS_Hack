"""Tests for ledger schema, init_db, seed idempotency, and money round-trip."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from ledger.schema import SCHEMA_VERSION, cents_to_money, connect, init_db, money_to_cents
from ledger.seed import seed_db
from shared.money import Money


@pytest.fixture()
def tmp_db_path() -> str:
    """Return a temp DB path that is cleaned up after the test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    Path(path).unlink(missing_ok=True)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture()
def seeded_conn(tmp_db_path: str) -> sqlite3.Connection:
    """Return a connection to a freshly seeded DB."""
    conn = connect(tmp_db_path)
    init_db(conn)
    seed_db(conn)
    yield conn
    conn.close()


class TestInitDb:
    def test_init_creates_all_tables(self, tmp_db_path: str) -> None:
        conn = connect(tmp_db_path)
        init_db(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        expected = {
            "users",
            "kyc",
            "accounts",
            "account_holders",
            "payees",
            "payee_aliases",
            "billers",
            "saved_billers",
            "cards",
            "holdings",
            "equity_quotes",
            "mandates",
            "transactions",
            "receipts",
            "schema_meta",
        }
        assert expected.issubset(table_names)

    def test_init_is_idempotent(self, tmp_db_path: str) -> None:
        conn = connect(tmp_db_path)
        init_db(conn)
        init_db(conn)
        # Still works
        rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        assert rows[0] == 0
        conn.close()

    def test_schema_version_recorded(self, tmp_db_path: str) -> None:
        conn = connect(tmp_db_path)
        init_db(conn)
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION
        conn.close()


class TestSeedIdempotency:
    def test_seed_twice_identical(self, tmp_db_path: str) -> None:
        conn = connect(tmp_db_path)
        init_db(conn)
        seed_db(conn)

        # Snapshot row counts and balances
        tables = [
            "users",
            "kyc",
            "accounts",
            "account_holders",
            "payees",
            "payee_aliases",
            "billers",
            "saved_billers",
            "cards",
            "holdings",
            "equity_quotes",
            "mandates",
        ]
        counts_1 = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        balances_1 = {
            r["account_id"]: r["balance_cents"]
            for r in conn.execute("SELECT account_id, balance_cents FROM accounts").fetchall()
        }

        seed_db(conn)

        counts_2 = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        balances_2 = {
            r["account_id"]: r["balance_cents"]
            for r in conn.execute("SELECT account_id, balance_cents FROM accounts").fetchall()
        }

        assert counts_1 == counts_2
        assert balances_1 == balances_2
        conn.close()

    def test_seed_users(self, seeded_conn: sqlite3.Connection) -> None:
        rows = seeded_conn.execute("SELECT user_id FROM users").fetchall()
        ids = {r["user_id"] for r in rows}
        assert ids == {"user-matthew", "user-ana"}

    def test_seed_matthew_accounts(self, seeded_conn: sqlite3.Connection) -> None:
        from ledger.schema import cents_to_money

        rows = seeded_conn.execute(
            "SELECT account_id, balance_cents FROM accounts WHERE account_id LIKE 'acct-matthew%'"
        ).fetchall()
        balances = {r["account_id"]: cents_to_money(r["balance_cents"]) for r in rows}
        assert balances["acct-matthew-savings"].to_str() == "12000.00"
        assert balances["acct-matthew-current"].to_str() == "3500.00"

    def test_seed_joint_account(self, seeded_conn: sqlite3.Connection) -> None:
        # Joint account should be held by both users
        holders = seeded_conn.execute(
            "SELECT user_id, role FROM account_holders WHERE account_id='acct-joint-01'"
        ).fetchall()
        holder_map = {r["user_id"]: r["role"] for r in holders}
        assert holder_map == {"user-matthew": "owner", "user-ana": "joint"}

        row = seeded_conn.execute(
            "SELECT balance_cents FROM accounts WHERE account_id='acct-joint-01'"
        ).fetchone()
        assert cents_to_money(row["balance_cents"]).to_str() == "2000.00"

    def test_seed_card_outstanding(self, seeded_conn: sqlite3.Connection) -> None:
        row = seeded_conn.execute(
            "SELECT outstanding_cents FROM cards WHERE card_id='card-matthew-01'"
        ).fetchone()
        assert cents_to_money(row["outstanding_cents"]).to_str() == "642.30"

    def test_seed_kyc(self, seeded_conn: sqlite3.Connection) -> None:
        mat = seeded_conn.execute(
            "SELECT status, tier, expires_at FROM kyc WHERE user_id='user-matthew'"
        ).fetchone()
        assert mat["status"] == "verified"
        assert mat["tier"] == 2
        assert mat["expires_at"].startswith("2027-12-31")

        ana = seeded_conn.execute(
            "SELECT status, tier FROM kyc WHERE user_id='user-ana'"
        ).fetchone()
        assert ana["status"] == "verified"
        assert ana["tier"] == 1

    def test_seed_mandate(self, seeded_conn: sqlite3.Connection) -> None:
        row = seeded_conn.execute(
            "SELECT version, status, per_leg_cents, daily_cents, allowed_actions "
            "FROM mandates WHERE user_id='user-matthew'"
        ).fetchone()
        assert row["version"] == 1
        assert row["status"] == "active"
        assert cents_to_money(row["per_leg_cents"]).to_str() == "1000.00"
        assert cents_to_money(row["daily_cents"]).to_str() == "3000.00"
        assert "transfer" in row["allowed_actions"]
        assert "buy_equity" in row["allowed_actions"]

    def test_seed_sanctioned_payee(self, seeded_conn: sqlite3.Connection) -> None:
        row = seeded_conn.execute(
            "SELECT display_name, sanctioned FROM payees WHERE payee_id='payee-northwind'"
        ).fetchone()
        assert row["display_name"] == "Northwind Trading"
        assert row["sanctioned"] == 1

    def test_seed_equity_quotes(self, seeded_conn: sqlite3.Connection) -> None:
        rows = seeded_conn.execute(
            "SELECT symbol, unit_price_cents, quoted_at, expires_at FROM equity_quotes"
        ).fetchall()
        symbols = {r["symbol"] for r in rows}
        assert symbols == {"D05", "O39", "U11", "Z74"}

        for r in rows:
            price = cents_to_money(r["unit_price_cents"])
            assert price.to_str() in {"38.50", "9.55", "28.30", "12.90"}
            # Quote expires 5 minutes after quoted_at
            quoted = r["quoted_at"]
            expires = r["expires_at"]
            assert quoted.startswith("2026-10-10T00:00:00")
            assert expires.startswith("2026-10-10T00:05:00")


class TestMoneyRoundTrip:
    """Money round-trips exactly through INTEGER cents."""

    @pytest.mark.parametrize(
        "money_str",
        ["0.01", "642.30", "12000.00", "999999.99", "0.00", "100.00"],
    )
    def test_round_trip(self, money_str: str) -> None:
        m = Money(money_str)
        cents = money_to_cents(m)
        assert cents == int(Money(money_str).value * 100)
        back = cents_to_money(cents)
        assert back.to_str() == money_str

    def test_no_real_columns(self, tmp_db_path: str) -> None:
        """Assert money columns are INTEGER, never REAL, via PRAGMA table_info."""
        conn = connect(tmp_db_path)
        init_db(conn)

        # Check all money-bearing columns are INTEGER
        money_columns = {
            "accounts": ["balance_cents"],
            "cards": ["outstanding_cents"],
            "equity_quotes": ["unit_price_cents"],
            "mandates": ["per_leg_cents", "daily_cents"],
            "transactions": ["amount_cents"],
        }
        for table, cols in money_columns.items():
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_types = {r["name"]: r["type"] for r in info}
            for col in cols:
                assert col_types[col] == "INTEGER", (
                    f"{table}.{col} should be INTEGER, got {col_types[col]}"
                )
        conn.close()
