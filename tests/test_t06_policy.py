"""T-06 deterministic hard policy: every boundary, with an injected ``now``.

Envelopes are built directly and evaluated against a freshly seeded ledger
(Matthew: KYC verified tier 2, savings 12,000 / current 3,500 / joint 2,000,
card outstanding 642.30, mandate v1 1,000 per leg / 3,000 per day / 4 legs).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gateway.policy import REASON_CODES, daily_debit_cents, singapore_day_bounds
from gateway.scam_signals import SCAM_SIGNAL_CODES, scam_signals
from ledger.schema import _SCHEMA_DDL, SCHEMA_VERSION, connect, init_db
from shared.money import Money
from shared.schemas import EquityTerms
from shared.timeutil import to_iso
from tests.t06_support import (
    CARD,
    CURRENT,
    JOINT,
    T0,
    add_history,
    db_fixture,  # noqa: F401  (registers the "db" fixture)
    evaluate,
    mk_envelope,
    mk_leg,
)


def _sql(db_path: str, statement: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(statement, params)


def _decide(db_path: str, *legs, now: datetime = T0, **kw):
    out = evaluate(db_path, mk_envelope(list(legs), now, **kw), now)
    return out.decision, out.reason_codes


# ── Mandate per-leg and daily limits ───────────────────────────────


def test_per_leg_limit_boundary(db: str) -> None:
    # 1000.00 is inside the hard limit (only above the step-up threshold).
    assert _decide(db, mk_leg("1000.00")) == ("STEP_UP", ("RISK_THRESHOLD_EXCEEDED",))
    assert _decide(db, mk_leg("1000.01")) == ("BLOCK", ("MANDATE_PER_LEG_LIMIT",))


def test_daily_aggregate_boundary_counts_history_and_bundle(db: str) -> None:
    add_history(db, "2500.00", T0 - timedelta(hours=1))
    assert _decide(db, mk_leg("300.00"), mk_leg("200.00", target_id="payee-ahma")) == ("PASS", ())
    assert _decide(db, mk_leg("300.00"), mk_leg("200.01", target_id="payee-ahma")) == (
        "BLOCK",
        ("MANDATE_DAILY_LIMIT",),
    )


def test_mandate_limits_come_from_the_row(db: str) -> None:
    _sql(db, "UPDATE mandates SET per_leg_cents = 20000, daily_cents = 30000")
    assert _decide(db, mk_leg("200.00"))[0] == "PASS"
    assert _decide(db, mk_leg("200.01"))[1] == ("MANDATE_PER_LEG_LIMIT",)
    assert _decide(db, mk_leg("200.00"), mk_leg("100.01", target_id="payee-ahma"))[1] == (
        "MANDATE_DAILY_LIMIT",
    )


def test_own_account_transfers_count_toward_daily_limit(db: str) -> None:
    add_history(db, "2900.00", T0 - timedelta(hours=1), action="transfer")
    own = mk_leg("100.01", target_id=CURRENT, target_type="own_account")
    assert _decide(db, own)[1] == ("MANDATE_DAILY_LIMIT",)


def test_kyc_tier_ceiling_is_stricter_than_mandate(db: str) -> None:
    _sql(db, "UPDATE kyc SET tier = 1 WHERE user_id = 'user-matthew'")  # SGD 500/day
    add_history(db, "300.00", T0 - timedelta(hours=2))
    assert _decide(db, mk_leg("200.00")) == ("PASS", ())
    assert _decide(db, mk_leg("200.01")) == ("BLOCK", ("KYC_TIER_DAILY_LIMIT",))


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("UPDATE kyc SET expires_at = '2026-11-02T03:59:59.999Z'", "KYC_EXPIRED"),
        ("UPDATE kyc SET status = 'expired'", "KYC_EXPIRED"),
        ("UPDATE kyc SET status = 'unverified'", "KYC_NOT_VERIFIED"),
        ("DELETE FROM kyc WHERE user_id = 'user-matthew'", "KYC_NOT_VERIFIED"),
    ],
    ids=["kyc-past-expiry", "kyc-status-expired", "kyc-unverified", "kyc-missing"],
)
def test_kyc_must_be_verified_and_current(db: str, statement: str, code: str) -> None:
    _sql(db, statement)
    assert _decide(db, mk_leg("10.00")) == ("BLOCK", (code,))


def test_kyc_expiry_boundary(db: str) -> None:
    _sql(db, "UPDATE kyc SET expires_at = ?", (to_iso(T0 + timedelta(milliseconds=1)),))
    assert _decide(db, mk_leg("10.00"))[0] == "PASS"
    assert _decide(db, mk_leg("10.00"), now=T0 + timedelta(milliseconds=1))[1] == ("KYC_EXPIRED",)


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("UPDATE mandates SET status = 'paused'", "MANDATE_PAUSED"),
        ("UPDATE mandates SET expires_at = '2026-11-02T04:00:00.000Z'", "MANDATE_EXPIRED"),
        ("UPDATE mandates SET status = 'pending'", "NO_ACTIVE_MANDATE"),
        ("UPDATE mandates SET allowed_actions = 'pay_bill,pay_card'", "MANDATE_ACTION_NOT_ALLOWED"),
    ],
    ids=["paused", "expired", "none-active", "action-not-allowed"],
)
def test_mandate_must_be_active_unexpired_and_allow_the_action(
    db: str, statement: str, code: str
) -> None:
    _sql(db, f"{statement} WHERE user_id = 'user-matthew'")
    assert _decide(db, mk_leg("10.00")) == ("BLOCK", (code,))


def test_paused_newer_version_wins_over_older_active(db: str) -> None:
    _sql(
        db,
        "INSERT INTO mandates (mandate_id, user_id, version, status, per_leg_cents, "
        "daily_cents, allowed_actions, effective_at, created_at) VALUES "
        "('m-v2', 'user-matthew', 2, 'paused', 100000, 300000, 'transfer', ?, ?)",
        (to_iso(T0), to_iso(T0)),
    )
    assert _decide(db, mk_leg("10.00"))[1] == ("MANDATE_PAUSED",)


def test_leg_count_limits(db: str) -> None:
    _sql(db, "UPDATE mandates SET max_legs = 2")
    legs = [mk_leg("1.00", target_id=t) for t in ("payee-mum", "payee-ahma", "payee-lim")]
    assert _decide(db, *legs) == ("BLOCK", ("MANDATE_MAX_LEGS",))
    _sql(db, "UPDATE mandates SET max_legs = 4")
    assert _decide(db, *legs)[0] == "PASS"
    # Five legs cannot even be built as a FrozenBundle; policy still blocks them.
    five = [mk_leg("1.00", action="pay_bill", target_type="biller", target_id="biller-spgroup")]
    five *= 5
    assert _decide(db, *five, construct=True) == ("BLOCK", ("MANDATE_MAX_LEGS",))


# ── Ownership, beneficiaries, sanctions ────────────────────────────


def test_sanctioned_payee_blocks(db: str) -> None:
    assert _decide(db, mk_leg("10.00", target_id="payee-northwind")) == (
        "BLOCK",
        ("PAYEE_SANCTIONED",),
    )


@pytest.mark.parametrize(
    ("leg_kwargs", "code"),
    [
        ({"target_id": "payee-ana-dad"}, "PAYEE_NOT_SAVED"),  # another user's payee
        ({"target_id": "payee-unknown"}, "PAYEE_NOT_SAVED"),
        ({"target_id": "payee-priya"}, "PAYEE_NOT_SAVED"),  # PayNow payee as bank payee
        ({"source": "acct-ana-savings"}, "SOURCE_ACCOUNT_NOT_OWNED"),
        (
            {"target_id": "acct-ana-savings", "target_type": "own_account"},
            "TARGET_ACCOUNT_NOT_OWNED",
        ),
        (
            {"target_id": "acct-matthew-savings", "target_type": "own_account"},
            "SAME_ACCOUNT_TRANSFER",
        ),
        (
            {"action": "pay_card", "target_type": "owned_card", "target_id": "card-ana-01"},
            "CARD_NOT_OWNED",
        ),
        (
            {"action": "pay_bill", "target_type": "biller", "target_id": "biller-unsaved"},
            "BILLER_NOT_SAVED",
        ),
    ],
    ids=[
        "other-users-payee",
        "unknown-payee",
        "paynow-kind-mismatch",
        "source-not-held",
        "target-account-not-held",
        "same-account",
        "card-not-owned",
        "biller-not-saved",
    ],
)
def test_targets_must_be_saved_or_owned(db: str, leg_kwargs: dict, code: str) -> None:
    assert _decide(db, mk_leg("10.00", **leg_kwargs)) == ("BLOCK", (code,))


def test_saved_paynow_and_joint_account_are_allowed(db: str) -> None:
    paynow = mk_leg("10.00", target_id="payee-priya", target_type="saved_paynow")
    joint = mk_leg("10.00", source=JOINT, target_id="payee-ahma")
    assert _decide(db, paynow, joint) == ("PASS", ())


# ── Funds and card outstanding (ordered simulation) ────────────────


def test_later_legs_may_spend_earlier_credits_but_not_overspend(db: str) -> None:
    _sql(db, "UPDATE accounts SET balance_cents = 1000 WHERE account_id = ?", (CURRENT,))  # 10.00
    top_up = mk_leg("100.00", target_id=CURRENT, target_type="own_account")
    spend = mk_leg("105.00", source=CURRENT, target_id="payee-mum")
    assert _decide(db, top_up, spend) == ("PASS", ())
    assert _decide(db, spend, top_up)[1] == ("INSUFFICIENT_FUNDS",)  # order matters
    overspend = mk_leg("5.01", source=CURRENT, target_id="payee-ahma")
    assert _decide(db, top_up, spend, overspend) == ("BLOCK", ("INSUFFICIENT_FUNDS",))


def test_exact_balance_is_sufficient(db: str) -> None:
    _sql(db, "UPDATE accounts SET balance_cents = 25000 WHERE account_id = ?", (JOINT,))
    assert _decide(db, mk_leg("250.00", source=JOINT))[0] == "PASS"
    assert _decide(db, mk_leg("250.01", source=JOINT))[1] == ("INSUFFICIENT_FUNDS",)


def test_card_overpayment_blocks(db: str) -> None:
    def card(amount: str):
        return mk_leg(amount, action="pay_card", target_type="owned_card", target_id=CARD)

    assert _decide(db, card("500.00"))[0] == "PASS"
    assert _decide(db, card("500.00"), card("142.30")) == ("STEP_UP", ("RISK_THRESHOLD_EXCEEDED",))
    assert _decide(db, card("642.31"))[1] == ("CARD_OVERPAYMENT",)
    assert _decide(db, card("500.00"), card("142.31"))[1] == ("CARD_OVERPAYMENT",)


# ── Quotes ─────────────────────────────────────────────────────────


def _quote(db_path: str, *, symbol: str = "D05", price_cents: int = 3850, ttl_min: int = 5):
    quote_id = f"quote-{symbol}-unit"
    expires = to_iso(T0 + timedelta(minutes=ttl_min))
    _sql(
        db_path,
        "INSERT INTO equity_quotes (quote_id, symbol, unit_price_cents, quoted_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (quote_id, symbol, price_cents, to_iso(T0), expires),
    )
    return quote_id, expires


def _equity_leg(quote_id: str, expires: str, *, symbol: str = "D05", price: str = "38.50"):
    terms = EquityTerms(
        budget=Money("100.00"),
        quote_id=quote_id,
        unit_price=Money(price),
        quoted_at=to_iso(T0),
        quote_expires_at=expires,
        quantity=int(Money("100.00").value // Money(price).value),
        actual_debit=Money(price) * int(Money("100.00").value // Money(price).value),
        residual_cash=Money("100.00")
        - Money(price) * int(Money("100.00").value // Money(price).value),
    )
    return mk_leg(
        terms.actual_debit.to_str(),
        action="buy_equity",
        target_type="sgx_symbol",
        target_id=symbol,
        equity_terms=terms,
    )


def test_quote_must_exist_match_and_be_unexpired(db: str) -> None:
    quote_id, expires = _quote(db)
    leg = _equity_leg(quote_id, expires)
    assert _decide(db, leg) == ("PASS", ())
    assert _decide(db, leg, now=T0 + timedelta(minutes=5))[1] == ("QUOTE_EXPIRED",)
    assert _decide(db, _equity_leg("quote-missing", expires))[1] == ("QUOTE_NOT_FOUND",)
    assert _decide(db, _equity_leg(quote_id, expires, symbol="O39"))[1] == (
        "QUOTE_SYMBOL_MISMATCH",
    )
    assert _decide(db, _equity_leg(quote_id, expires, price="38.00"))[1] == ("QUOTE_TERMS_CHANGED",)


# ── Velocity (rolling ten minutes, transfer legs only) ────────────


def test_velocity_counts_committed_and_candidate_transfer_legs(db: str) -> None:
    for minutes in (9, 8, 3):
        add_history(db, "1.00", T0 - timedelta(minutes=minutes), action="transfer")
    two = [mk_leg("1.00"), mk_leg("1.00", target_id="payee-ahma")]
    assert _decide(db, *two)[0] == "PASS"  # 3 + 2 = 5
    three = [*two, mk_leg("1.00", target_id="payee-lim")]
    assert _decide(db, *three) == ("BLOCK", ("VELOCITY_LIMIT",))  # 3 + 3 = 6
    bill = mk_leg("1.00", action="pay_bill", target_type="biller", target_id="biller-spgroup")
    assert _decide(db, bill, bill.model_copy(update={"leg_id": "b2"}))[0] == "PASS"


def test_velocity_window_is_rolling_ten_minutes(db: str) -> None:
    for _ in range(5):
        add_history(db, "1.00", T0, action="transfer")
    one = mk_leg("1.00")
    assert _decide(db, one, now=T0 + timedelta(minutes=9, seconds=59))[1] == ("VELOCITY_LIMIT",)
    # Exactly ten minutes later the oldest legs have left the window.
    assert _decide(db, one, now=T0 + timedelta(minutes=10))[0] == "PASS"
    assert _decide(db, one, now=T0 + timedelta(minutes=11))[0] == "PASS"


# ── Asia/Singapore calendar days ───────────────────────────────────

SGT_2359 = datetime(2026, 11, 2, 15, 59, tzinfo=UTC)  # 23:59 SGT, 2 Nov
SGT_0001 = datetime(2026, 11, 2, 16, 1, tzinfo=UTC)  # 00:01 SGT, 3 Nov


def test_singapore_day_bounds() -> None:
    assert singapore_day_bounds(SGT_2359) == (
        "2026-11-01T16:00:00.000Z",
        "2026-11-02T16:00:00.000Z",
    )
    assert singapore_day_bounds(SGT_0001) == (
        "2026-11-02T16:00:00.000Z",
        "2026-11-03T16:00:00.000Z",
    )


def test_debits_either_side_of_sg_midnight_count_on_different_days(db: str) -> None:
    add_history(db, "2900.00", SGT_2359 - timedelta(minutes=30))
    add_history(db, "100.00", SGT_2359)  # 23:59 SGT, 2 Nov
    add_history(db, "40.00", SGT_0001)  # 00:01 SGT, 3 Nov
    with sqlite3.connect(db) as conn:
        assert daily_debit_cents(conn, "user-matthew", SGT_2359) == 300_000
        assert daily_debit_cents(conn, "user-matthew", SGT_0001) == 4_000
    # 2 Nov is full; the same payment a minute after SG midnight passes.
    assert _decide(db, mk_leg("0.01"), now=SGT_2359 + timedelta(seconds=30))[1] == (
        "MANDATE_DAILY_LIMIT",
    )
    assert _decide(db, mk_leg("400.00"), now=SGT_0001)[0] == "PASS"
    # The UTC date is 2 Nov for both instants: a UTC day would have wrongly merged them.
    assert SGT_2359.date() == SGT_0001.date()


# ── Step-up and precedence ─────────────────────────────────────────


def test_risk_threshold_boundary(db: str) -> None:
    assert _decide(db, mk_leg("300.00"), mk_leg("200.00", target_id="payee-ahma"))[0] == "PASS"
    assert _decide(db, mk_leg("300.00"), mk_leg("200.01", target_id="payee-ahma")) == (
        "STEP_UP",
        ("RISK_THRESHOLD_EXCEEDED",),
    )


def test_block_beats_step_up_and_reports_only_block_codes(db: str) -> None:
    decision, codes = _decide(db, mk_leg("900.00", target_id="payee-northwind"))
    assert (decision, codes) == ("BLOCK", ("PAYEE_SANCTIONED",))


def test_multiple_block_codes_are_all_reported(db: str) -> None:
    _sql(db, "UPDATE kyc SET status = 'expired'")
    _sql(db, "UPDATE mandates SET status = 'paused'")
    decision, codes = _decide(db, mk_leg("1000.01", target_id="payee-northwind"))
    assert decision == "BLOCK"
    assert set(codes) == {
        "KYC_EXPIRED",
        "MANDATE_PAUSED",
        "MANDATE_PER_LEG_LIMIT",
        "PAYEE_SANCTIONED",
    }


def test_mandate_update_has_no_payment_policy(db: str) -> None:
    from gateway.policy import evaluate as policy_evaluate
    from shared.schemas import MandateChange, SigningEnvelope

    env = SigningEnvelope(
        operation="update_mandate",
        user_id="user-matthew",
        credential_id="cred",
        operation_id="change-1",
        payload=MandateChange(paused=True),
        active_mandate_version=1,
        nonce="cd" * 32,
        issued_at=to_iso(T0),
        expires_at=to_iso(T0 + timedelta(seconds=60)),
    )
    conn = connect(db)
    try:
        assert policy_evaluate(conn, env, T0).reason_codes == ("UNSUPPORTED_OPERATION",)
    finally:
        conn.close()


def test_every_emitted_code_is_registered() -> None:
    assert set(SCAM_SIGNAL_CODES) <= set(REASON_CODES)


# ── Scam signals (deterministic, gateway-held text) ────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Move 900 to my safe account right now",
            {"SCAM_SIGNAL_SAFE_ACCOUNT", "SCAM_SIGNAL_URGENCY"},
        ),
        ("The police officer told me to transfer it", {"SCAM_SIGNAL_IMPERSONATION"}),
        ("A bank officer said I must move my savings", {"SCAM_SIGNAL_IMPERSONATION"}),
        ("Pay Mum 50, don’t tell anyone", {"SCAM_SIGNAL_SECRECY"}),
        (
            "Keep this confidential and do it immediately",
            {"SCAM_SIGNAL_SECRECY", "SCAM_SIGNAL_URGENCY"},
        ),
        ("He asked me to install AnyDesk first", {"SCAM_SIGNAL_REMOTE_ACCESS"}),
    ],
)
def test_scam_phrases_fire(text: str, expected: set[str]) -> None:
    assert set(scam_signals([text])) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Send 50 to Mum from my savings",
        "Pay my SP Group bill urgently please",  # urgency alone never fires
        "Transfer 100 to my savings account",
        "Is my account safe? Pay the card anyway",  # words apart, not the phrase
    ],
)
def test_benign_requests_do_not_fire(text: str) -> None:
    assert scam_signals([text]) == ()


def test_known_false_positive_is_documented_behaviour() -> None:
    # A benign surprise still reads as secrecy: one extra step-up, never a block.
    assert scam_signals(["Keep this secret, it is a surprise party for Mum"]) == (
        "SCAM_SIGNAL_SECRECY",
    )


def test_phrases_do_not_join_across_turns() -> None:
    assert scam_signals(["send it to the safe", "account ending 4521"]) == ()


# ── Schema migration ───────────────────────────────────────────────


def test_v3_database_migrates_to_v4_idempotently(tmp_path) -> None:
    path = str(tmp_path / "v3.db")
    conn = connect(path)
    conn.executescript(_SCHEMA_DDL)  # the v3 baseline shape
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '3')")
    conn.commit()
    init_db(conn)
    init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert {"authorization_id", "leg_id", "direction", "quantity"} <= cols
    assert {"expires_at", "max_legs"} <= {r[1] for r in conn.execute("PRAGMA table_info(mandates)")}
    receipt_cols = {r[1] for r in conn.execute("PRAGMA table_info(receipts)")}
    assert {"operation", "operation_id", "payload_hash", "receipt_json"} <= receipt_cols
    assert conn.execute("SELECT value FROM schema_meta").fetchone()[0] == str(SCHEMA_VERSION)
    conn.close()


def test_receipt_idempotency_key_is_unique(db: str) -> None:
    from shared.timeutil import now_utc

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        row = ("op-1", "hash", "env", "{}", to_iso(now_utc()))
        conn.execute(
            "INSERT INTO receipts (receipt_id, authorization_id, user_id, operation, operation_id, "
            "payload_hash, envelope_hash, receipt_json, created_at) "
            "VALUES ('r1', 'a1', 'user-matthew', 'execute_bundle', ?, ?, ?, ?, ?)",
            row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipts (receipt_id, authorization_id, user_id, operation, "
                "operation_id, payload_hash, envelope_hash, receipt_json, created_at) "
                "VALUES ('r2', 'a2', 'user-matthew', 'execute_bundle', ?, ?, ?, ?, ?)",
                row,
            )
