"""Deterministic hard policy for signed operations (T-06).

:func:`evaluate` returns ``PASS``, ``STEP_UP`` or ``BLOCK`` with stable reason
codes for one :class:`~shared.schemas.SigningEnvelope`, reading only gateway
state and an explicit ``now``.  No model output is consulted.  The same
function runs at envelope issuance, after the passkey signature, and again
under the execution write lock (``gateway.execution``), where its answer is
final.

Rules for ``execute_bundle`` (all amounts are prototype defaults, not DBS or
regulatory rules):

- **KYC**: status ``verified`` and not past ``expires_at``.  Tier daily
  ceilings 500 / 5,000 / 20,000 SGD.
- **Mandate**: the user's highest-version mandate that is ``active`` or
  ``paused`` (pending increases are ignored until activated).  It must be
  active, unexpired, allow every leg's action, allow the leg count, and its
  per-leg and daily limits come from the mandate row.  The stricter of the
  KYC tier ceiling and the mandate daily limit applies (both are checked).
- **Spending counts every outgoing leg**, own-account transfers included:
  the daily total is the sum of all committed debit legs in the current
  Asia/Singapore calendar day plus every leg of the candidate bundle.  This is
  deliberately conservative; moving money between your own accounts uses up
  the daily mandate.
- **Ownership**: each source account is held by the user; each target is the
  user's own account (not the source), saved payee (bank or PayNow, matching
  the target type), saved biller or owned card.  Sanctioned payees block.
- **Quotes**: equity legs need the stored quote to exist, match the symbol and
  frozen unit price/expiry, and be unexpired at ``now``.
- **Funds**: legs are simulated in order.  Debits need sufficient balance at
  that point; an own-account transfer credits its destination so later legs
  may spend it; a card payment may not exceed the card's (simulated)
  outstanding balance.
- **Velocity**: at most five transfer legs in any rolling ten minutes,
  counting committed transfer debits after ``now - 10 min`` plus the
  candidate's transfer legs.

Within all hard limits, ``STEP_UP`` applies when the bundle's total debit
exceeds the risk threshold (default 500.00) or a gateway-derived scam signal
fires (``gateway.scam_signals``).  **BLOCK always wins**; a BLOCK outcome
carries only block codes and nothing downstream can override it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any

from gateway.approval.hooks import PolicyOutcome
from gateway.scam_signals import conversation_scam_signals
from ledger.schema import money_to_cents
from shared.money import Money
from shared.schemas import MAX_LEGS, Action, FrozenBundle, SigningEnvelope, TargetType
from shared.timeutil import from_iso, to_iso

__all__ = [
    "PASS",
    "STEP_UP",
    "BLOCK",
    "REASON_CODES",
    "KYC_TIER_DAILY_CENTS",
    "VELOCITY_MAX_TRANSFER_LEGS",
    "VELOCITY_WINDOW",
    "MandateRow",
    "evaluate",
    "current_mandate",
    "active_mandate_version",
    "daily_debit_cents",
    "recent_transfer_legs",
    "singapore_day_bounds",
]

PASS = "PASS"
STEP_UP = "STEP_UP"
BLOCK = "BLOCK"

#: KYC tier → daily debit ceiling in cents (SGD 500 / 5,000 / 20,000).
KYC_TIER_DAILY_CENTS: dict[int, int] = {1: 50_000, 2: 500_000, 3: 2_000_000}

VELOCITY_MAX_TRANSFER_LEGS = 5
VELOCITY_WINDOW = timedelta(minutes=10)

#: Stable reason codes.  The UI keys on these codes, never on prose.
REASON_CODES: dict[str, str] = {
    # BLOCK
    "UNSUPPORTED_OPERATION": "This operation type has no payment policy.",
    "KYC_NOT_VERIFIED": "Identity verification is missing or not verified.",
    "KYC_EXPIRED": "Identity verification has expired.",
    "KYC_TIER_DAILY_LIMIT": "Today's debits would exceed the KYC tier daily ceiling.",
    "NO_ACTIVE_MANDATE": "There is no active spending mandate.",
    "MANDATE_PAUSED": "The conversational spending mandate is paused.",
    "MANDATE_EXPIRED": "The spending mandate has expired.",
    "MANDATE_ACTION_NOT_ALLOWED": "The mandate does not allow this action.",
    "MANDATE_MAX_LEGS": "The bundle has more legs than the mandate allows.",
    "MANDATE_PER_LEG_LIMIT": "A leg exceeds the mandate's per-leg limit.",
    "MANDATE_DAILY_LIMIT": "Today's debits would exceed the mandate's daily limit.",
    "SOURCE_ACCOUNT_NOT_OWNED": "A source account is not held by the user.",
    "TARGET_ACCOUNT_NOT_OWNED": "A destination account is not held by the user.",
    "SAME_ACCOUNT_TRANSFER": "A transfer's source and destination are the same account.",
    "PAYEE_NOT_SAVED": "The recipient is not one of the user's saved payees.",
    "PAYEE_SANCTIONED": "The recipient is on the (mock) sanctions list.",
    "BILLER_NOT_SAVED": "The biller is not saved by the user.",
    "CARD_NOT_OWNED": "The card is not owned by the user.",
    "CARD_OVERPAYMENT": "A card payment exceeds the card's outstanding balance.",
    "QUOTE_NOT_FOUND": "The equity quote does not exist.",
    "QUOTE_EXPIRED": "The equity quote has expired; a new draft is required.",
    "QUOTE_SYMBOL_MISMATCH": "The quote is for a different symbol.",
    "QUOTE_TERMS_CHANGED": "The stored quote no longer matches the approved terms.",
    "INSUFFICIENT_FUNDS": "A leg would overdraw its source account.",
    "VELOCITY_LIMIT": "More than five transfer legs in ten minutes.",
    # STEP_UP
    "RISK_THRESHOLD_EXCEEDED": "The bundle total is above the step-up threshold.",
    "SCAM_SIGNAL_SAFE_ACCOUNT": "The conversation mentions a 'safe account'.",
    "SCAM_SIGNAL_IMPERSONATION": "The conversation suggests an official is giving instructions.",
    "SCAM_SIGNAL_SECRECY": "The conversation asks for secrecy.",
    "SCAM_SIGNAL_REMOTE_ACCESS": "The conversation mentions remote-access software.",
    "SCAM_SIGNAL_URGENCY": "The conversation adds urgency to another scam signal.",
}

_SINGAPORE_FALLBACK = timezone(timedelta(hours=8), "SGT")


def _singapore_tz() -> tzinfo:
    """Asia/Singapore; a fixed UTC+8 fallback if the tz database is missing
    (Singapore has had no offset change since 1982)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Singapore")
    except Exception:  # ZoneInfoNotFoundError or a missing tzdata package
        return _SINGAPORE_FALLBACK


def singapore_day_bounds(now: datetime) -> tuple[str, str]:
    """UTC ISO bounds ``[start, end)`` of the Asia/Singapore calendar day containing *now*."""
    tz = _singapore_tz()
    local = now.astimezone(tz)
    start_local = datetime(local.year, local.month, local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return to_iso(start_local.astimezone(UTC)), to_iso(end_local.astimezone(UTC))


def daily_debit_cents(conn: sqlite3.Connection, user_id: str, now: datetime) -> int:
    """Committed debit legs by *user_id* in the Asia/Singapore day containing *now*."""
    start, end = singapore_day_bounds(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
        "WHERE user_id = ? AND direction = 'debit' AND created_at >= ? AND created_at < ?",
        (user_id, start, end),
    ).fetchone()
    return int(row[0])


def recent_transfer_legs(conn: sqlite3.Connection, user_id: str, now: datetime) -> int:
    """Committed transfer debit legs by *user_id* after ``now - VELOCITY_WINDOW``."""
    row = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND direction = 'debit' "
        "AND action = 'transfer' AND created_at > ?",
        (user_id, to_iso(now - VELOCITY_WINDOW)),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True)
class MandateRow:
    version: int
    status: str
    per_leg_cents: int
    daily_cents: int
    allowed_actions: frozenset[str]
    max_legs: int
    expires_at: str | None


def current_mandate(conn: sqlite3.Connection, user_id: str) -> MandateRow | None:
    """The user's highest-version mandate that is ``active`` or ``paused``.

    A pause recorded on a newer version therefore wins over an older active
    one.  ``pending`` rows (cooling increases) are ignored until activated.
    """
    row = conn.execute(
        "SELECT version, status, per_leg_cents, daily_cents, allowed_actions, max_legs, "
        "expires_at FROM mandates WHERE user_id = ? AND status IN ('active', 'paused') "
        "ORDER BY version DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return MandateRow(
        version=int(row[0]),
        status=row[1],
        per_leg_cents=int(row[2]),
        daily_cents=int(row[3]),
        allowed_actions=frozenset(a.strip() for a in row[4].split(",") if a.strip()),
        max_legs=int(row[5]),
        expires_at=row[6],
    )


def active_mandate_version(conn: sqlite3.Connection, user_id: str) -> int | None:
    """Version of the current mandate if it is active (not paused), else ``None``."""
    mandate = current_mandate(conn, user_id)
    return mandate.version if mandate is not None and mandate.status == "active" else None


# ── Evaluation ─────────────────────────────────────────────────────


class _Codes:
    """Ordered, de-duplicated reason codes."""

    def __init__(self) -> None:
        self._codes: list[str] = []

    def add(self, code: str) -> None:
        if code not in REASON_CODES:
            raise KeyError(f"unregistered reason code: {code}")
        if code not in self._codes:
            self._codes.append(code)

    def extend(self, codes: Iterable[str]) -> None:
        for code in codes:
            self.add(code)

    def __bool__(self) -> bool:
        return bool(self._codes)

    def tuple(self) -> tuple[str, ...]:
        return tuple(self._codes)


def _outcome(decision: str, codes: tuple[str, ...]) -> PolicyOutcome:
    return PolicyOutcome(decision=decision, reason=",".join(codes), reason_codes=codes)


def _default_risk_threshold() -> Money:
    from shared.settings import load_gateway_settings

    return Money(load_gateway_settings().risk_threshold)


def evaluate(
    conn: sqlite3.Connection,
    envelope: SigningEnvelope,
    now: datetime,
    *,
    risk_threshold: Money | None = None,
) -> PolicyOutcome:
    """Evaluate the hard policy for *envelope* at *now*.  Reads only; never writes."""
    if envelope.operation != "execute_bundle" or not isinstance(envelope.payload, FrozenBundle):
        # Mandate updates have their own operation-specific policy
        # (gateway.mandates.evaluate_change); this payment policy never passes them.
        return _outcome(BLOCK, ("UNSUPPORTED_OPERATION",))

    bundle = envelope.payload
    user_id = envelope.user_id
    blocks = _Codes()
    total_cents = sum(money_to_cents(leg.amount) for leg in bundle.legs)

    _check_kyc(conn, user_id, now, total_cents, blocks)
    _check_mandate(conn, user_id, bundle, now, total_cents, blocks)
    _check_legs(conn, user_id, bundle, now, blocks)
    _check_velocity(conn, user_id, bundle, now, blocks)

    if blocks:
        return _outcome(BLOCK, blocks.tuple())

    step_up = _Codes()
    threshold = risk_threshold if risk_threshold is not None else _default_risk_threshold()
    if total_cents > money_to_cents(threshold):
        step_up.add("RISK_THRESHOLD_EXCEEDED")
    if envelope.conversation_id:
        step_up.extend(conversation_scam_signals(conn, envelope.conversation_id))
    if step_up:
        return _outcome(STEP_UP, step_up.tuple())
    return _outcome(PASS, ())


def _check_kyc(
    conn: sqlite3.Connection, user_id: str, now: datetime, total_cents: int, blocks: _Codes
) -> None:
    row = conn.execute(
        "SELECT status, tier, expires_at FROM kyc WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None or row[0] == "unverified":
        blocks.add("KYC_NOT_VERIFIED")
        return
    if row[0] == "expired" or (row[2] is not None and from_iso(row[2]) <= now):
        blocks.add("KYC_EXPIRED")
        return
    if row[0] != "verified":
        blocks.add("KYC_NOT_VERIFIED")
        return
    ceiling = KYC_TIER_DAILY_CENTS.get(int(row[1]), 0)
    if daily_debit_cents(conn, user_id, now) + total_cents > ceiling:
        blocks.add("KYC_TIER_DAILY_LIMIT")


def _check_mandate(
    conn: sqlite3.Connection,
    user_id: str,
    bundle: FrozenBundle,
    now: datetime,
    total_cents: int,
    blocks: _Codes,
) -> None:
    mandate = current_mandate(conn, user_id)
    if mandate is None:
        blocks.add("NO_ACTIVE_MANDATE")
        return
    if mandate.status == "paused":
        blocks.add("MANDATE_PAUSED")
    if mandate.expires_at is not None and from_iso(mandate.expires_at) <= now:
        blocks.add("MANDATE_EXPIRED")
    if len(bundle.legs) > min(mandate.max_legs, MAX_LEGS):
        blocks.add("MANDATE_MAX_LEGS")
    for leg in bundle.legs:
        if leg.action.value not in mandate.allowed_actions:
            blocks.add("MANDATE_ACTION_NOT_ALLOWED")
        if money_to_cents(leg.amount) > mandate.per_leg_cents:
            blocks.add("MANDATE_PER_LEG_LIMIT")
    if daily_debit_cents(conn, user_id, now) + total_cents > mandate.daily_cents:
        blocks.add("MANDATE_DAILY_LIMIT")


def _held_accounts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT a.account_id, a.balance_cents FROM accounts a "
        "JOIN account_holders h ON h.account_id = a.account_id WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()
    return {row[0]: int(row[1]) for row in rows}


def _check_legs(
    conn: sqlite3.Connection,
    user_id: str,
    bundle: FrozenBundle,
    now: datetime,
    blocks: _Codes,
) -> None:
    """Ownership, beneficiary, quote and ordered funds simulation for every leg."""
    balances = _held_accounts(conn, user_id)
    card_outstanding: dict[str, int] = {}

    for leg in bundle.legs:
        amount = money_to_cents(leg.amount)
        source_ok = leg.source_account_id in balances
        if not source_ok:
            blocks.add("SOURCE_ACCOUNT_NOT_OWNED")

        credit_to: str | None = None
        tt = leg.target_type
        if tt == TargetType.own_account:
            if leg.target_id not in balances:
                blocks.add("TARGET_ACCOUNT_NOT_OWNED")
            elif leg.target_id == leg.source_account_id:
                blocks.add("SAME_ACCOUNT_TRANSFER")
            else:
                credit_to = leg.target_id
        elif tt in (TargetType.saved_payee, TargetType.saved_paynow):
            _check_payee(conn, user_id, tt, leg.target_id, blocks)
        elif tt == TargetType.biller:
            saved = conn.execute(
                "SELECT 1 FROM saved_billers WHERE user_id = ? AND biller_id = ?",
                (user_id, leg.target_id),
            ).fetchone()
            if saved is None:
                blocks.add("BILLER_NOT_SAVED")
        elif tt == TargetType.owned_card:
            if leg.target_id not in card_outstanding:
                card = conn.execute(
                    "SELECT outstanding_cents FROM cards WHERE card_id = ? AND owner_user_id = ?",
                    (leg.target_id, user_id),
                ).fetchone()
                if card is None:
                    blocks.add("CARD_NOT_OWNED")
                else:
                    card_outstanding[leg.target_id] = int(card[0])
            if leg.target_id in card_outstanding:
                if amount > card_outstanding[leg.target_id]:
                    blocks.add("CARD_OVERPAYMENT")
                else:
                    card_outstanding[leg.target_id] -= amount
        elif tt == TargetType.sgx_symbol:
            _check_quote(conn, leg, now, blocks)

        # Ordered funds simulation: debit now, credit an own destination after.
        if source_ok:
            if balances[leg.source_account_id] < amount:
                blocks.add("INSUFFICIENT_FUNDS")
            else:
                balances[leg.source_account_id] -= amount
                if credit_to is not None:
                    balances[credit_to] += amount


def _check_payee(
    conn: sqlite3.Connection, user_id: str, tt: TargetType, payee_id: str, blocks: _Codes
) -> None:
    kind = "paynow" if tt == TargetType.saved_paynow else "bank"
    row = conn.execute(
        "SELECT sanctioned FROM payees WHERE payee_id = ? AND owner_user_id = ? AND kind = ?",
        (payee_id, user_id, kind),
    ).fetchone()
    if row is None:
        blocks.add("PAYEE_NOT_SAVED")
    elif int(row[0]):
        blocks.add("PAYEE_SANCTIONED")


def _check_quote(conn: sqlite3.Connection, leg: Any, now: datetime, blocks: _Codes) -> None:
    terms = leg.equity_terms
    if terms is None:  # the schema forbids this; fail closed anyway
        blocks.add("QUOTE_NOT_FOUND")
        return
    quote = conn.execute(
        "SELECT symbol, unit_price_cents, expires_at FROM equity_quotes WHERE quote_id = ?",
        (terms.quote_id,),
    ).fetchone()
    if quote is None:
        blocks.add("QUOTE_NOT_FOUND")
        return
    if str(quote[0]).upper() != leg.target_id.upper():
        blocks.add("QUOTE_SYMBOL_MISMATCH")
    if int(quote[1]) != money_to_cents(terms.unit_price) or quote[2] != terms.quote_expires_at:
        blocks.add("QUOTE_TERMS_CHANGED")
    if min(from_iso(quote[2]), from_iso(terms.quote_expires_at)) <= now:
        blocks.add("QUOTE_EXPIRED")
    if terms.actual_debit.value != terms.unit_price.value * Decimal(terms.quantity):
        blocks.add("QUOTE_TERMS_CHANGED")


def _check_velocity(
    conn: sqlite3.Connection, user_id: str, bundle: FrozenBundle, now: datetime, blocks: _Codes
) -> None:
    candidate = sum(1 for leg in bundle.legs if leg.action == Action.transfer)
    if candidate and recent_transfer_legs(conn, user_id, now) + candidate > (
        VELOCITY_MAX_TRANSFER_LEGS
    ):
        blocks.add("VELOCITY_LIMIT")
