"""Pydantic response models for the gateway bank read API.

Money is serialized as two-decimal strings via the :class:`Money`
Pydantic integration.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from shared.money import Money


class _BankBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountResponse(_BankBase):
    account_id: str
    type: str
    masked_suffix: str
    balance: Money
    role: str
    created_at: str


class PayeeResponse(_BankBase):
    payee_id: str
    display_name: str
    bank: str | None = None
    masked_suffix: str | None = None
    kind: str
    sanctioned: bool = False
    #: Bank aliases and customer nicknames (exact names for this payee).
    names: list[str] = []
    #: "exact", or "fuzzy" for a near spelling the agent may only suggest.
    match: str = "exact"


class BillerResponse(_BankBase):
    biller_id: str
    display_name: str
    category: str | None = None
    bill_reference: str | None = None
    saved_at: str


class CardResponse(_BankBase):
    card_id: str
    masked_suffix: str
    outstanding: Money


class HoldingResponse(_BankBase):
    holding_id: str
    symbol: str
    quantity: int
    updated_at: str


class QuoteResponse(_BankBase):
    quote_id: str
    symbol: str
    unit_price: Money
    quoted_at: str
    expires_at: str


class TransactionResponse(_BankBase):
    transaction_id: str
    account_id: str
    action: str
    target_type: str
    target_id: str
    amount: Money
    direction: str = "debit"  # "debit" | "credit" (own-account transfer destination)
    status: str
    created_at: str


class QuoteIssueRequest(_BankBase):
    symbol: str = Field(..., min_length=1, max_length=10)
