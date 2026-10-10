"""Scoped bank read API routes under ``/internal/bank/...``.

All endpoints require ``bank:read`` scope and filter to ``principal.user_id``.
Another user's resource IDs return 404 (not 403) to avoid existence leaks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from gateway.auth.delegation import Principal, require_principal
from gateway.bank import MAX_LIST_SIZE, MAX_QUERY_LENGTH, BankStore
from gateway.bank_models import (
    AccountResponse,
    BillerResponse,
    CardResponse,
    HoldingResponse,
    PayeeResponse,
    QuoteIssueRequest,
    QuoteResponse,
    TransactionResponse,
)

router = APIRouter(prefix="/internal/bank", tags=["bank-read"])

# Module-level store instance; initialized lazily.
_store: BankStore | None = None


def get_bank_store() -> BankStore:
    global _store  # noqa: PLW0603
    if _store is None:
        from shared.settings import load_gateway_settings

        _store = BankStore(load_gateway_settings().db_path)
    return _store


def _set_bank_store(store: BankStore) -> None:
    """Test override: inject a store with a temp DB path."""
    global _store  # noqa: PLW0603
    _store = store


# Pre-built dependency for bank:read scope — avoids B008 (Depends in defaults).
BankReadPrincipal = Annotated[Principal, Depends(require_principal("bank:read"))]


# ── Accounts ────────────────────────────────────────────────────────


@router.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(principal: BankReadPrincipal) -> list[dict]:
    store = get_bank_store()
    return store.get_accounts(principal.user_id)


@router.get("/accounts/{account_id}", response_model=AccountResponse)
async def get_account(account_id: str, principal: BankReadPrincipal) -> dict:
    store = get_bank_store()
    account = store.get_account(principal.user_id, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    return account


# ── Payees ──────────────────────────────────────────────────────────


@router.get("/payees/search", response_model=list[PayeeResponse])
async def search_payees(
    principal: BankReadPrincipal,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
) -> list[dict]:
    store = get_bank_store()
    return store.search_payees(principal.user_id, q)


# ── Billers ─────────────────────────────────────────────────────────


@router.get("/billers", response_model=list[BillerResponse])
async def list_billers(principal: BankReadPrincipal) -> list[dict]:
    store = get_bank_store()
    return store.get_billers(principal.user_id)


# ── Cards ──────────────────────────────────────────────────────────


@router.get("/cards", response_model=list[CardResponse])
async def list_cards(principal: BankReadPrincipal) -> list[dict]:
    store = get_bank_store()
    return store.get_cards(principal.user_id)


@router.get("/cards/{card_id}", response_model=CardResponse)
async def get_card(card_id: str, principal: BankReadPrincipal) -> dict:
    store = get_bank_store()
    card = store.get_card(principal.user_id, card_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    return card


# ── Holdings ────────────────────────────────────────────────────────


@router.get("/holdings", response_model=list[HoldingResponse])
async def list_holdings(principal: BankReadPrincipal) -> list[dict]:
    store = get_bank_store()
    return store.get_holdings(principal.user_id)


# ── Quotes ──────────────────────────────────────────────────────────


@router.post("/quotes", response_model=QuoteResponse)
async def issue_quote(body: QuoteIssueRequest, principal: BankReadPrincipal) -> dict:
    """Issue a new mock equity quote.

    POST is the only write-ish endpoint: it creates a new quote row.
    Quotes are never mutated; expired quotes stay expired.
    """
    from gateway.clock import utcnow

    store = get_bank_store()
    try:
        return store.get_quote(body.symbol, now=utcnow())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None


@router.get("/quotes/{quote_id}", response_model=QuoteResponse)
async def get_quote(quote_id: str, principal: BankReadPrincipal) -> dict:
    store = get_bank_store()
    quote = store.fetch_quote(quote_id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="quote not found")
    # Report expired quotes but still return the data.
    return quote


@router.get("/quotes/{quote_id}/status")
async def get_quote_status(quote_id: str, principal: BankReadPrincipal) -> dict:
    """Check if a quote is expired.  Returns ``{"expired": true/false}``."""
    from gateway.clock import utcnow
    from shared.timeutil import from_iso

    store = get_bank_store()
    quote = store.fetch_quote(quote_id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="quote not found")
    expires = from_iso(quote["expires_at"])
    now = utcnow()
    return {"quote_id": quote_id, "expired": now >= expires, "expires_at": quote["expires_at"]}


# ── Transactions ───────────────────────────────────────────────────


@router.get("/transactions", response_model=list[TransactionResponse])
async def list_transactions(
    principal: BankReadPrincipal,
    limit: int = Query(default=MAX_LIST_SIZE, ge=1, le=MAX_LIST_SIZE),
) -> list[dict]:
    store = get_bank_store()
    return store.get_transactions(principal.user_id, limit=limit)
