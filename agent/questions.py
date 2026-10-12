"""Clarification questions, written by code.

Questions only contain fixed wording, the user's own words (located verbatim in their
turns, shortened) and gateway bank labels. They never contain model-generated text.
The UI must still escape them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

_MAX_WORDS_LEN = 40


@dataclass(frozen=True)
class Issue:
    """Why the agent cannot propose yet, and the one question to ask about it."""

    code: str
    question: str
    leg: int | None = None
    field: str | None = None
    candidates: list[dict[str, Any]] = dc_field(default_factory=list)


def quote_words(text: str) -> str:
    """The user's words, single-line and bounded, in curly quotes."""
    flat = " ".join(text.split())
    if len(flat) > _MAX_WORDS_LEN:
        flat = flat[: _MAX_WORDS_LEN - 1].rstrip() + "…"
    return f"“{flat}”"


def join_options(options: Sequence[str]) -> str:
    if len(options) <= 1:
        return "".join(options)
    return ", ".join(options[:-1]) + " or " + options[-1]


NO_INTENT = (
    "What would you like to do? I can send money to your saved payees or your own accounts, "
    "pay a saved bill, pay your card or buy SGX shares."
)
UNSUPPORTED_ACTION = (
    "I can only send money to saved payees or your own accounts, pay saved bills, pay your "
    "card or buy SGX shares. Which of these would you like to do?"
)
EXTRACTION_RESTATE = (
    "I couldn't match your request to your own words. Could you say it again in one message, "
    "with the amount, who to pay and which account to use?"
)


def too_many_legs(count: int, limit: int) -> str:
    return (
        f"I can prepare up to {limit} payments at a time and I counted {count}. "
        f"Which {limit} should I prepare first? You can ask for the rest afterwards."
    )


def amount_missing(payment: str) -> str:
    return f"How much should I use for {payment}?"


def amount_unsupported_currency(payment: str) -> str:
    return (
        f"I can only make payments in Singapore dollars. How much in SGD should I use for "
        f"{payment}?"
    )


def amount_unclear(payment: str, words: str) -> str:
    return (
        f"I need an exact amount in SGD for {payment}; I couldn't use {quote_words(words)}. "
        "How much exactly?"
    )


def amount_out_of_range(payment: str) -> str:
    return f"That amount is more than this service can handle. How much should I use for {payment}?"


def amount_needs_value(payment: str) -> str:
    return f"I can't look up that amount for {payment}. How much exactly should I pay?"


def relative_no_earlier(words: str) -> str:
    return (
        f"{quote_words(words)} must refer to an earlier payment in the same request, and I "
        "didn't find one. How much exactly should I use?"
    )


def relative_ambiguous(words: str, options: Sequence[str]) -> str:
    return f"When you said {quote_words(words)}, which amount do you mean: {join_options(options)}?"


def recipient_missing(payment: str) -> str:
    return f"Who should receive {payment}?"


def recipient_not_found(words: str, options: Sequence[str] | None = None) -> str:
    base = f"I couldn't find {quote_words(words)} among your saved payees, billers or accounts."
    if options:
        return f"{base} Which one do you mean: {join_options(options)}?"
    return f"{base} Who should I pay? I can only pay saved payees or your own accounts."


#: Last option of a "Did you mean …?" question, so the customer can reject every suggestion.
NONE_OF_THESE = "none of these"


def recipient_did_you_mean(words: str, options: Sequence[str]) -> str:
    """Near spellings of the user's words. The UI turns the options into answer chips."""
    return (
        f"I couldn't find {quote_words(words)} exactly. "
        f"Did you mean: {join_options([*options, NONE_OF_THESE])}?"
    )


def recipient_ambiguous(words: str, options: Sequence[str]) -> str:
    return f"Which {quote_words(words)} do you mean: {join_options(options)}?"


def recipient_uncitable(words: str) -> str:
    return (
        f"I couldn't tell exactly who {quote_words(words)} is. "
        "Please give the full saved name or the last 4 digits of their account."
    )


def source_missing(payment: str, accounts: Sequence[str]) -> str:
    return f"Which account should I use for {payment}: {join_options(accounts)}?"


def source_unclear(words: str, accounts: Sequence[str]) -> str:
    return f"Which account do you mean by {quote_words(words)}: {join_options(accounts)}?"


def same_account(payment: str) -> str:
    return (
        f"The source and destination are the same account for {payment}. "
        "Which account should the money come from?"
    )


def quote_not_found(payment: str) -> str:
    return (
        f"I couldn't match part of {payment} to your exact words. "
        "Could you say the amount, who to pay and which account to use again?"
    )


def quote_in_reference(payment: str) -> str:
    return (
        "I don't take amounts, payees or accounts from a payment reference or quoted text. "
        f"Please tell me directly what to do for {payment}: how much, to whom and from "
        "which account."
    )


def reference_too_long(limit: int) -> str:
    return f"Payment references can be at most {limit} characters. What reference should I use?"


def card_nothing_outstanding(card: str) -> str:
    return f"There is nothing outstanding on {card}. How much would you like to pay, if anything?"


def gateway_recipient(payment: str) -> str:
    return (
        f"I couldn't confirm who should receive {payment} from your words. "
        "Please say their full saved name or the last 4 digits of their account."
    )


def gateway_source(accounts: Sequence[str]) -> str:
    if accounts:
        return f"Which account should the money come from: {join_options(accounts)}?"
    return "Which of your accounts should the money come from?"


def gateway_amount_reference(payment: str) -> str:
    return f"I can't take an amount from a payment reference. How much should I pay for {payment}?"


def gateway_relative() -> str:
    return "Which earlier payment should that amount be based on? Or tell me the exact amount."
