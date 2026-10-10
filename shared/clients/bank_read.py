"""Read-only HTTP client for the gateway bank read API.

The agent uses this client to fetch account balances, payee candidates,
billers, cards, holdings, quotes and transaction history.  No execute/
write methods exist outside the gateway.  The only POST is quote issue,
which creates a new immutable quote row.

The delegation token is passed in the ``Authorization: Bearer`` header.
The caller can never choose the user; the user comes only from the
verified token.
"""

from __future__ import annotations

import httpx

__all__ = ["BankReadClient"]


class BankReadClient:
    """Scoped read-only HTTP client for the gateway bank API.

    All methods take a delegation token.  The token determines the
    authenticated user; no method accepts a user_id parameter.
    """

    def __init__(self, base_url: str = "http://localhost:8002", *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ── Accounts ────────────────────────────────────────────────────

    def list_accounts(self, token: str) -> list[dict]:
        """List all accounts the token's user holds."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/accounts",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_account(self, token: str, account_id: str) -> dict:
        """Get a single account by ID."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/accounts/{account_id}",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Payees ──────────────────────────────────────────────────────

    def search_payees(self, token: str, query: str) -> list[dict]:
        """Search payees by name/alias.  Returns ALL matching candidates."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/payees/search",
            headers=self._headers(token),
            params={"q": query},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Billers ─────────────────────────────────────────────────────

    def list_billers(self, token: str) -> list[dict]:
        """List saved billers with bill references."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/billers",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Cards ───────────────────────────────────────────────────────

    def list_cards(self, token: str) -> list[dict]:
        """List owned cards with outstanding balance."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/cards",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_card(self, token: str, card_id: str) -> dict:
        """Get a single card by ID."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/cards/{card_id}",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Holdings ────────────────────────────────────────────────────

    def list_holdings(self, token: str) -> list[dict]:
        """List equity holdings."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/holdings",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Quotes ──────────────────────────────────────────────────────

    def issue_quote(self, token: str, symbol: str) -> dict:
        """Issue a new mock equity quote.

        This is the only POST method.  It creates a new immutable quote
        row; it does not execute or modify any financial state.
        """
        resp = httpx.post(
            f"{self._base_url}/internal/bank/quotes",
            headers=self._headers(token),
            json={"symbol": symbol},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_quote(self, token: str, quote_id: str) -> dict:
        """Fetch a quote by ID."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/quotes/{quote_id}",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_quote_status(self, token: str, quote_id: str) -> dict:
        """Check if a quote is expired."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/quotes/{quote_id}/status",
            headers=self._headers(token),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Transactions ─────────────────────────────────────────────────

    def list_transactions(self, token: str, limit: int = 50) -> list[dict]:
        """List recent transaction history."""
        resp = httpx.get(
            f"{self._base_url}/internal/bank/transactions",
            headers=self._headers(token),
            params={"limit": limit},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()
