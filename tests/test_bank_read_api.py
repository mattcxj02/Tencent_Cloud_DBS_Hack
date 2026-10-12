"""Tests for the scoped bank read API on the gateway.

Uses FastAPI TestClient with a temp DB.  Tests cover:
- Cross-user reads return 404 (not 403).
- Payee search returns ALL matching candidates.
- Quote issue/fetch with expiry.
- User cannot be overridden by headers.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.auth.delegation import DelegationIssuer, _set_delegation_issuer
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from shared.timeutil import from_iso

TEST_SECRET = "test-secret-key-0123456789abcdefghijklmnop"
OTHER_SECRET = "other-secret-key-0123456789abcdefghijklmno"


@pytest.fixture()
def bank_env():
    """Set up a temp DB, seed it, and wire the BankStore into the app."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    Path(db_path).unlink(missing_ok=True)

    store = BankStore(db_path)
    store.init_and_seed()
    _set_bank_store(store)

    # Issuer with a fixed secret for testing
    issuer = DelegationIssuer(secret=TEST_SECRET)
    _set_delegation_issuer(issuer)

    yield store, issuer

    Path(db_path).unlink(missing_ok=True)
    _set_bank_store(None)  # type: ignore[arg-type]
    _set_delegation_issuer(None)


@pytest.fixture()
def matthew_token(bank_env) -> str:
    _, issuer = bank_env
    return issuer.issue_token("user-matthew", ["bank:read"])


@pytest.fixture()
def ana_token(bank_env) -> str:
    _, issuer = bank_env
    return issuer.issue_token("user-ana", ["bank:read"])


@pytest.fixture()
def write_token(bank_env) -> str:
    _, issuer = bank_env
    return issuer.issue_token("user-matthew", ["bank:write"])


class TestAccounts:
    def test_matthew_accounts(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 200
        accounts = resp.json()
        ids = {a["account_id"] for a in accounts}
        assert "acct-matthew-savings" in ids
        assert "acct-matthew-current" in ids
        assert "acct-joint-01" in ids
        assert "acct-ana-savings" not in ids

        # Check balances
        savings = next(a for a in accounts if a["account_id"] == "acct-matthew-savings")
        assert savings["balance"] == "12000.00"
        assert savings["type"] == "savings"
        assert savings["role"] == "owner"

        joint = next(a for a in accounts if a["account_id"] == "acct-joint-01")
        assert joint["balance"] == "2000.00"
        assert joint["role"] == "owner"

    def test_ana_sees_joint_account(self, ana_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts",
            headers={"Authorization": f"Bearer {ana_token}"},
        )
        assert resp.status_code == 200
        accounts = resp.json()
        ids = {a["account_id"] for a in accounts}
        assert "acct-ana-savings" in ids
        assert "acct-joint-01" in ids
        assert "acct-matthew-savings" not in ids

        joint = next(a for a in accounts if a["account_id"] == "acct-joint-01")
        assert joint["role"] == "joint"

    def test_cross_user_account_404(self, ana_token: str) -> None:
        """Ana cannot read Matthew's account — 404, not 403."""
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts/acct-matthew-savings",
            headers={"Authorization": f"Bearer {ana_token}"},
        )
        assert resp.status_code == 404
        assert "account not found" in resp.json()["detail"]

    def test_no_token_401(self) -> None:
        client = TestClient(app)
        resp = client.get("/internal/bank/accounts")
        assert resp.status_code == 401

    def test_wrong_scope_403(self, write_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts",
            headers={"Authorization": f"Bearer {write_token}"},
        )
        assert resp.status_code == 403


class TestPayeeSearch:
    @pytest.mark.parametrize("q", ["%", "_", "%%", "j_hn"])
    def test_like_wildcards_match_literally(self, matthew_token: str, q: str) -> None:
        """Wildcard characters are literal: they never match a payee exactly.

        "j_hn" may still come back as a near-spelling suggestion of John (``match:
        "fuzzy"``), which the agent can only offer as a question.
        """
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": q},
        )
        assert resp.status_code == 200
        assert [p for p in resp.json() if p["match"] == "exact"] == []
        assert all(p["display_name"].startswith("John") for p in resp.json())

    def test_search_john(self, matthew_token: str) -> None:
        """Search for 'john' returns both John Doe and John Tan."""
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "john"},
        )
        assert resp.status_code == 200
        payees = resp.json()
        names = [p["display_name"] for p in payees]
        assert "John Doe" in names
        assert "John Tan" in names
        assert len(payees) >= 2

    def test_exact_alias_mum(self, matthew_token: str) -> None:
        """Exact alias match for 'Mum'."""
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "Mum"},
        )
        assert resp.status_code == 200
        payees = resp.json()
        assert len(payees) >= 1
        assert payees[0]["display_name"] == "Mum"

    def test_alias_ah_ma(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "Ah Ma"},
        )
        assert resp.status_code == 200
        payees = resp.json()
        assert len(payees) >= 1
        assert payees[0]["display_name"] == "Ah Ma"

    def test_northwind_sanctioned(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "Northwind"},
        )
        assert resp.status_code == 200
        payees = resp.json()
        assert len(payees) >= 1
        assert payees[0]["display_name"] == "Northwind Trading"
        assert payees[0]["sanctioned"] is True

    def test_empty_query_rejected(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": ""},
        )
        assert resp.status_code == 422  # min_length=1

    def test_oversized_query_rejected(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "x" * 201},
        )
        assert resp.status_code == 422  # max_length=200

    def test_cross_user_payees(self, ana_token: str, matthew_token: str) -> None:
        """Ana cannot see Matthew's payees."""
        client = TestClient(app)
        # Ana searching "john" should find nothing (her payees are Dad, Sis)
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {ana_token}"},
            params={"q": "john"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

        # Matthew searching "dad" should find nothing
        resp = client.get(
            "/internal/bank/payees/search",
            headers={"Authorization": f"Bearer {matthew_token}"},
            params={"q": "Dad"},
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestCards:
    def test_matthew_card(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/cards",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 200
        cards = resp.json()
        assert len(cards) == 1
        assert cards[0]["card_id"] == "card-matthew-01"
        assert cards[0]["outstanding"] == "642.30"

    def test_cross_user_card_404(self, ana_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/cards/card-matthew-01",
            headers={"Authorization": f"Bearer {ana_token}"},
        )
        assert resp.status_code == 404


class TestBillers:
    def test_matthew_billers(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/billers",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 200
        billers = resp.json()
        names = {b["display_name"] for b in billers}
        assert "SP Group" in names
        sp = next(b for b in billers if b["display_name"] == "SP Group")
        assert sp["bill_reference"] == "SP-REF-2026-001"


class TestHoldings:
    def test_matthew_holdings(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/holdings",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 200
        holdings = resp.json()
        assert len(holdings) == 1
        assert holdings[0]["symbol"] == "D05"
        assert holdings[0]["quantity"] == 100


class TestQuotes:
    def test_issue_quote(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.post(
            "/internal/bank/quotes",
            headers={"Authorization": f"Bearer {matthew_token}"},
            json={"symbol": "D05"},
        )
        assert resp.status_code == 200
        quote = resp.json()
        assert quote["symbol"] == "D05"
        assert quote["unit_price"] == "38.50"
        assert quote["expires_at"] > quote["quoted_at"]

    def test_issue_and_fetch_quote(self, matthew_token: str) -> None:
        client = TestClient(app)
        # Issue
        resp = client.post(
            "/internal/bank/quotes",
            headers={"Authorization": f"Bearer {matthew_token}"},
            json={"symbol": "Z74"},
        )
        assert resp.status_code == 200
        quote = resp.json()
        quote_id = quote["quote_id"]
        assert quote["unit_price"] == "12.90"

        # Fetch
        resp = client.get(
            f"/internal/bank/quotes/{quote_id}",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 200
        fetched = resp.json()
        assert fetched["quote_id"] == quote_id
        assert fetched["unit_price"] == "12.90"

    def test_quote_expires_5_minutes(self, bank_env) -> None:
        store, _ = bank_env
        now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        quote = store.get_quote("D05", now=now)
        quoted = from_iso(quote["quoted_at"])
        expires = from_iso(quote["expires_at"])
        delta = (expires - quoted).total_seconds()
        assert delta == 300  # 5 minutes

    def test_expired_quote_status(self, bank_env) -> None:
        """An expired quote should report expired status."""
        store, _ = bank_env
        # Create a quote that expired in the past
        past = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        quote = store.get_quote("O39", now=past)
        quote_id = quote["quote_id"]

        # Check status
        client = TestClient(app)
        issuer = bank_env[1]
        token = issuer.issue_token("user-matthew", ["bank:read"])
        resp = client.get(
            f"/internal/bank/quotes/{quote_id}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["expired"] is True

    def test_unsupported_symbol_400(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.post(
            "/internal/bank/quotes",
            headers={"Authorization": f"Bearer {matthew_token}"},
            json={"symbol": "XYZ"},
        )
        assert resp.status_code == 400

    def test_quote_not_found_404(self, matthew_token: str) -> None:
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/quotes/quote-nonexistent",
            headers={"Authorization": f"Bearer {matthew_token}"},
        )
        assert resp.status_code == 404


class TestUserOverridePrevention:
    """The caller can never choose the user by header or query param."""

    def test_x_user_header_ignored(self, ana_token: str) -> None:
        """Even with X-User-Id: user-matthew, ana's token gives ana's data."""
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts",
            headers={
                "Authorization": f"Bearer {ana_token}",
                "X-User-Id": "user-matthew",
            },
        )
        assert resp.status_code == 200
        accounts = resp.json()
        ids = {a["account_id"] for a in accounts}
        assert "acct-matthew-savings" not in ids
        assert "acct-ana-savings" in ids

    def test_query_param_user_ignored(self, ana_token: str) -> None:
        """Query param ?user_id=user-matthew is ignored."""
        client = TestClient(app)
        resp = client.get(
            "/internal/bank/accounts",
            headers={"Authorization": f"Bearer {ana_token}"},
            params={"user_id": "user-matthew"},
        )
        assert resp.status_code == 200
        accounts = resp.json()
        ids = {a["account_id"] for a in accounts}
        assert "acct-matthew-savings" not in ids
        assert "acct-ana-savings" in ids
