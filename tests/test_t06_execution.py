"""T-06 atomic execution, step-up gating, idempotency and concurrency.

Every test drives the real routes, the real policy and the real commit on a
temporary database, with a frozen gateway clock.  Concurrency tests use real
threads; each commit opens its own SQLite connection and serializes on the
``BEGIN IMMEDIATE`` write lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from typing import Any

import pytest

from gateway.audit import verify_chain
from gateway.execution import ExecutionFailed, ExecutionRefused
from shared.canonical import approval_challenge_hex, canonical_bytes
from shared.money import Money
from shared.schemas import SigningEnvelope
from tests.t06_support import (
    CARD,
    CURRENT,
    JOINT,
    SAVINGS,
    T0,
    T6,
    t6_fixture,  # noqa: F401  (registers the "t6" fixture)
)

MUM_TEXT = "Send 50 to Mum from my savings"
MUM = {"amount": "50.00", "amount_words": "50", "target_id": "payee-mum", "target_words": "Mum"}


def _receipt(t: T6, authz_id: str):
    return t.env.client.get(f"/api/authorizations/{authz_id}/receipt")


def _race(*calls) -> list[Any]:
    """Run callables at the same instant on separate threads; return results or exceptions."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # collected for assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results


def _approved_pending(t: T6, text: str, legs: list[dict[str, Any]]) -> tuple[str, dict]:
    """Freeze, issue and record a verified approval without executing yet."""
    _, bundle_id = t.bundle(text, legs)
    view = t.issued(bundle_id)
    t.approve_without_commit(view)
    assert t.status(view["authorization_id"]) == "PENDING_APPROVAL"
    return bundle_id, view


# ── Effects ────────────────────────────────────────────────────────


def test_bundle_commits_every_effect_receipt_and_audit(t6: T6) -> None:
    quote = t6.quote("D05")
    text = (
        "Move 100 from my savings to my current, pay 100 on my card from current, "
        "pay SP Group 20 from savings and buy 300 of DBS from savings"
    )
    legs = [
        {"amount": "100.00", "amount_words": "100", "target_id": CURRENT,
         "target_type": "own_account", "target_words": "current"},
        {"amount": "100.00", "amount_words": "100", "amount_occ": 1, "action": "pay_card",
         "target_type": "owned_card", "target_id": CARD, "target_words": "card",
         "source_id": CURRENT, "source_words": "current", "source_occ": 1},
        {"amount": "20.00", "amount_words": "20", "action": "pay_bill", "target_type": "biller",
         "target_id": "biller-spgroup", "target_words": "SP Group", "source_occ": 1},
        {"amount": "300.00", "amount_words": "300", "action": "buy_equity",
         "target_type": "sgx_symbol", "target_id": "D05", "target_words": "DBS",
         "source_occ": 2, "equity_terms": {"quote_id": quote["quote_id"]}},
    ]  # fmt: skip
    _, bundle_id = t6.bundle(text, legs)
    view = t6.issued(bundle_id)
    resp = t6.approve(view)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["replayed"]) == ("COMMITTED", False)

    # Own-account transfer credits the destination; the card payment comes back out of it.
    assert t6.balance(CURRENT) == "3500.00"
    # Savings: -100 transfer, -20 bill, -269.50 equity (7 x 38.50, never the 300 budget).
    assert t6.balance(SAVINGS) == "11610.50"
    assert t6.outstanding(CARD) == "542.30"
    assert t6.holding("D05") == 107

    receipt = body["receipt"]
    assert receipt["total_debit"] == "489.50" and receipt["simulated"] is True
    by_leg = {leg["leg_id"]: leg for leg in receipt["legs"]}
    assert by_leg["leg-1"]["credit_transaction_id"]
    assert by_leg["leg-3"]["settlement_ref"].startswith("SIM-")
    assert (by_leg["leg-4"]["quantity"], by_leg["leg-4"]["amount"]) == (7, "269.50")
    assert t6.q(
        "SELECT direction, COUNT(*) FROM transactions WHERE authorization_id = ? "
        "GROUP BY direction ORDER BY direction",
        (view["authorization_id"],),
    ) == [("credit", 1), ("debit", 4)]

    assert t6.status(view["authorization_id"]) == "COMMITTED"
    assert t6.draft_status(bundle_id) == "EXECUTED"
    assert t6.count("receipts") == 1
    assert "gateway.execution.commit" in t6.audit_steps()
    with sqlite3.connect(t6.db) as conn:
        conn.row_factory = sqlite3.Row
        assert verify_chain(conn)["ok"]


def test_committed_debits_feed_the_daily_limit(t6: T6) -> None:
    t6.add_history("2920.00", T0 - timedelta(hours=1))
    assert t6.pay(MUM_TEXT, [MUM])["status"] == "COMMITTED"  # 2970.00 today
    resp = t6.issue(t6.bundle(MUM_TEXT, [MUM])[1])  # would make 3020.00
    assert (resp.status_code, resp.json()["detail"]) == (409, "POLICY_BLOCK")
    assert resp.json()["reason_codes"] == ["MANDATE_DAILY_LIMIT"]


# ── Step-up gating ─────────────────────────────────────────────────


def test_step_up_over_threshold_moves_no_money_until_verified(t6: T6) -> None:
    before = t6.balances()
    _, bundle_id = t6.bundle(
        "Send 600 to Mum from my savings",
        [{**MUM, "amount": "600.00", "amount_words": "600"}],
    )
    view = t6.issued(bundle_id)  # STEP_UP does not prevent issuance
    resp = t6.approve(view)
    assert resp.json() == {
        "authorization_id": view["authorization_id"],
        "status": "AWAITING_STEP_UP",
        "reason": "RISK_THRESHOLD_EXCEEDED",
        "reason_codes": ["RISK_THRESHOLD_EXCEEDED"],
    }
    assert t6.balances() == before and t6.count("transactions") == 0
    shown = t6.env.client.get(f"/api/authorizations/{view['authorization_id']}").json()
    assert (shown["status"], shown["reason_codes"]) == (
        "AWAITING_STEP_UP",
        ["RISK_THRESHOLD_EXCEEDED"],
    )

    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"])  # no step-up proof
    assert (refused.value.code, refused.value.status) == ("STEP_UP_REQUIRED", "AWAITING_STEP_UP")
    assert t6.balances() == before

    code = t6.step_up_code(view["authorization_id"])  # T-07 simulated channel
    receipt = t6.commit(view["authorization_id"], step_up_code=code)
    assert receipt.status == "COMMITTED" and receipt.details["step_up_verified"] is True
    assert t6.balance(SAVINGS) == "11400.00"


def test_step_up_proof_cannot_override_a_hard_block(t6: T6) -> None:
    _, bundle_id = t6.bundle(
        "Send 600 to Mum from my savings",
        [{**MUM, "amount": "600.00", "amount_words": "600"}],
    )
    view = t6.issued(bundle_id)
    assert t6.approve(view).json()["status"] == "AWAITING_STEP_UP"
    t6.sql("UPDATE accounts SET balance_cents = 59999 WHERE account_id = ?", (SAVINGS,))
    code = t6.step_up_code(view["authorization_id"])
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"], step_up_code=code)
    assert (refused.value.status, refused.value.reason_codes) == (
        "BLOCKED",
        ("INSUFFICIENT_FUNDS",),
    )
    assert t6.status(view["authorization_id"]) == "BLOCKED"
    assert t6.balance(SAVINGS) == "599.99" and t6.count("transactions") == 0


def test_scam_phrase_in_captured_conversation_triggers_step_up(t6: T6) -> None:
    text = "The police officer told me to send 50 to Mum from my savings and not to tell anyone"
    _, bundle_id = t6.bundle(text, [MUM])
    body = t6.approve(t6.issued(bundle_id)).json()
    assert body["status"] == "AWAITING_STEP_UP"
    assert body["reason_codes"] == ["SCAM_SIGNAL_IMPERSONATION", "SCAM_SIGNAL_SECRECY"]
    assert t6.count("transactions") == 0


def test_block_beats_step_up(t6: T6) -> None:
    text = "Urgent, the bank officer told me to send 900 to Northwind from my savings"
    leg = {"amount": "900.00", "amount_words": "900", "target_id": "payee-northwind",
           "target_words": "Northwind"}  # fmt: skip
    resp = t6.issue(t6.bundle(text, [leg])[1])
    assert resp.status_code == 409
    assert resp.json() == {"detail": "POLICY_BLOCK", "reason_codes": ["PAYEE_SANCTIONED"]}


# ── Atomicity ──────────────────────────────────────────────────────

THREE_TEXT = "Send 10 to Mum from savings, 20 to Ah Ma from savings and 30 to Lim from current"
THREE = [
    {"amount": "10.00", "amount_words": "10", "target_id": "payee-mum", "target_words": "Mum"},
    {"amount": "20.00", "amount_words": "20", "target_id": "payee-ahma", "target_words": "Ah Ma",
     "source_occ": 1},
    {"amount": "30.00", "amount_words": "30", "target_id": "payee-lim", "target_words": "Lim",
     "source_id": CURRENT, "source_words": "current"},
]  # fmt: skip


def test_balance_change_before_commit_blocks_whole_bundle(t6: T6) -> None:
    bundle_id, view = _approved_pending(t6, THREE_TEXT, THREE)
    t6.sql("UPDATE accounts SET balance_cents = 500 WHERE account_id = ?", (CURRENT,))
    before = t6.balances()
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"])
    assert (refused.value.status, refused.value.reason_codes) == (
        "BLOCKED",
        ("INSUFFICIENT_FUNDS",),
    )
    assert t6.balances() == before and t6.count("transactions") == 0
    assert t6.draft_status(bundle_id) == "DRAFT_FROZEN"


def test_third_leg_failure_rolls_back_earlier_legs(t6: T6) -> None:
    bundle_id, view = _approved_pending(t6, THREE_TEXT, THREE)
    before = t6.balances()
    # The third leg's settlement fails after legs 1-2 were written in the transaction.
    t6.sql(
        "CREATE TRIGGER fail_leg_3 BEFORE INSERT ON transactions WHEN NEW.leg_id = 'leg-3' "
        "BEGIN SELECT RAISE(ABORT, 'settlement unavailable'); END"
    )
    with pytest.raises(ExecutionFailed):
        t6.commit(view["authorization_id"])
    assert t6.balances() == before
    assert t6.count("transactions") == 0 and t6.count("receipts") == 0
    assert t6.status(view["authorization_id"]) == "PENDING_APPROVAL"
    assert t6.draft_status(bundle_id) == "DRAFT_FROZEN"
    assert "gateway.execution.commit" not in t6.audit_steps()

    t6.sql("DROP TRIGGER fail_leg_3")  # retry is safe: still exactly one execution
    assert t6.commit(view["authorization_id"]).status == "COMMITTED"
    assert t6.balance(SAVINGS) == "11970.00" and t6.balance(CURRENT) == "3470.00"
    assert t6.count("transactions") == 3


def test_audit_failure_rolls_back_all_effects(t6: T6) -> None:
    _, bundle_id = t6.bundle(THREE_TEXT, THREE)
    view = t6.issued(bundle_id)
    before = t6.balances()
    t6.sql(
        "CREATE TRIGGER fail_commit_audit BEFORE INSERT ON audit_events "
        "WHEN NEW.step = 'gateway.execution.commit' "
        "BEGIN SELECT RAISE(ABORT, 'audit store unavailable'); END"
    )
    resp = t6.approve(view)
    assert (resp.status_code, resp.json()["detail"]) == (503, "COMMIT_FAILED")
    assert t6.balances() == before
    assert t6.count("transactions") == 0 and t6.count("receipts") == 0
    assert t6.status(view["authorization_id"]) == "PENDING_APPROVAL"
    lost = _receipt(t6, view["authorization_id"])
    assert (lost.status_code, lost.json()["detail"], lost.json()["status"]) == (
        409,
        "NOT_COMMITTED",
        "PENDING_APPROVAL",
    )


# ── Expiry ─────────────────────────────────────────────────────────


def test_expired_approval_never_executes(t6: T6) -> None:
    _, view = _approved_pending(t6, MUM_TEXT, [MUM])
    t6.clock.advance(seconds=120)  # expires_at is issued_at + 120 s; the boundary is expired
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"])
    assert (refused.value.code, refused.value.status) == ("AUTHORIZATION_EXPIRED", "EXPIRED")
    assert t6.status(view["authorization_id"]) == "EXPIRED"
    assert t6.count("transactions") == 0 and t6.balance(SAVINGS) == "12000.00"


def test_correction_after_approval_supersedes_before_commit(t6: T6) -> None:
    conv, _ = t6.bundle(MUM_TEXT, [MUM])
    view = t6.issued(t6.q("SELECT bundle_id FROM frozen_drafts")[0][0])
    t6.approve_without_commit(view)
    t6.sql(  # simulate a correction landing after the signature check
        "UPDATE conversations SET revision = revision + 1 WHERE conversation_id = ?", (conv,)
    )
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"])
    assert refused.value.status == "SUPERSEDED"
    assert "CONVERSATION_CHANGED" in refused.value.reason_codes
    assert t6.count("transactions") == 0


# ── Idempotency and lost responses ─────────────────────────────────


def test_retries_return_the_original_receipt_without_new_effects(t6: T6) -> None:
    _, bundle_id = t6.bundle(MUM_TEXT, [MUM])
    view = t6.issued(bundle_id)
    first = t6.approve(view).json()
    assert first["status"] == "COMMITTED"
    after_first = (t6.balances(), t6.count("transactions"))

    # The response was lost: the client fetches the receipt, then retries approve.
    fetched = _receipt(t6, view["authorization_id"]).json()
    assert (fetched["receipt_id"], fetched["receipt"]) == (first["receipt_id"], first["receipt"])
    retry = t6.approve(view).json()
    assert (retry["receipt_id"], retry["replayed"]) == (first["receipt_id"], True)

    # Long after the approval window: still the same receipt, still no new effects.
    t6.clock.advance(minutes=30)
    late = t6.approve(view).json()
    assert (late["status"], late["receipt_id"]) == ("COMMITTED", first["receipt_id"])
    direct = t6.commit(view["authorization_id"])
    assert (direct.receipt_id, direct.replayed) == (first["receipt_id"], True)
    assert (t6.balances(), t6.count("transactions")) == after_first
    assert t6.count("receipts") == 1


def test_receipt_route_is_owner_only(t6: T6) -> None:
    _, bundle_id = t6.bundle(MUM_TEXT, [MUM])
    view = t6.issued(bundle_id)
    pending = _receipt(t6, view["authorization_id"])
    assert (pending.status_code, pending.json()["detail"]) == (409, "NOT_COMMITTED")
    t6.approve(view)
    t6.env.as_user("user-ana")
    assert _receipt(t6, view["authorization_id"]).status_code == 404
    assert _receipt(t6, "authz-does-not-exist").status_code == 404
    t6.env.as_user("user-matthew")
    assert _receipt(t6, view["authorization_id"]).status_code == 200


def test_second_authorization_for_a_committed_bundle_replays_the_receipt(t6: T6) -> None:
    _, bundle_id = t6.bundle(MUM_TEXT, [MUM])
    first, second = t6.issued(bundle_id), t6.issued(bundle_id)
    committed = t6.approve(first).json()
    replay = t6.approve(second).json()
    assert (replay["status"], replay["receipt_id"]) == ("COMMITTED", committed["receipt_id"])
    assert t6.status(second["authorization_id"]) == "SUPERSEDED"
    assert t6.count("transactions") == 1 and t6.balance(SAVINGS) == "11950.00"


def test_different_payload_under_the_same_key_is_rejected(t6: T6) -> None:
    _, bundle_id = t6.bundle(MUM_TEXT, [MUM])
    view = t6.issued(bundle_id)
    t6.approve(view)
    (raw,) = t6.q(
        "SELECT envelope_bytes FROM authorizations WHERE authorization_id = ?",
        (view["authorization_id"],),
    )[0]
    envelope = SigningEnvelope.model_validate(json.loads(raw))
    bundle = envelope.payload
    changed_leg = bundle.legs[0].model_copy(update={"amount": Money("51.00")})
    forged = envelope.model_copy(
        update={"payload": bundle.model_copy(update={"legs": [changed_leg]}), "nonce": "ef" * 32}
    )
    t6.sql(
        "INSERT INTO authorizations (authorization_id, bundle_id, user_id, credential_id, "
        "operation, envelope_bytes, envelope_hash, active_mandate_version, conversation_id, "
        "conversation_revision, status, issued_at, expires_at, created_at) "
        "VALUES ('authz-forged', ?, 'user-matthew', ?, 'execute_bundle', ?, ?, 1, ?, ?, "
        "'PENDING_APPROVAL', ?, ?, ?)",
        (
            bundle_id,
            forged.credential_id,
            canonical_bytes(forged),
            approval_challenge_hex(forged),
            forged.conversation_id,
            forged.conversation_revision,
            forged.issued_at,
            forged.expires_at,
            forged.issued_at,
        ),
    )
    t6.sql(
        "INSERT INTO approval_evidence (authorization_id, credential_id, sign_count, "
        "client_data_hash, authenticator_data_hash, verified_at) "
        "VALUES ('authz-forged', ?, 0, 'x', 'y', ?)",
        (forged.credential_id, forged.issued_at),
    )
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit("authz-forged")
    assert refused.value.code == "IDEMPOTENCY_CONFLICT"
    assert t6.count("transactions") == 1 and t6.balance(SAVINGS) == "11950.00"


def test_commit_is_scoped_to_the_authenticated_owner(t6: T6) -> None:
    _, view = _approved_pending(t6, MUM_TEXT, [MUM])
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"], user="user-ana")
    assert refused.value.code == "AUTHORIZATION_NOT_FOUND"
    assert t6.count("transactions") == 0


# ── Concurrency ────────────────────────────────────────────────────


def test_concurrent_approvals_of_one_authorization_commit_once(t6: T6) -> None:
    _, bundle_id = t6.bundle(MUM_TEXT, [MUM])
    view = t6.issued(bundle_id)
    responses = _race(*[lambda: t6.approve(view) for _ in range(4)])
    ok = [r.json() for r in responses if r.status_code == 200]
    assert ok and all(r["status"] == "COMMITTED" for r in ok)
    assert len({r["receipt_id"] for r in ok}) == 1
    assert {r.json()["detail"] for r in responses if r.status_code != 200} <= {"ALREADY_APPROVED"}
    assert t6.count("transactions") == 1 and t6.count("receipts") == 1
    assert t6.balance(SAVINGS) == "11950.00"


def _two_pending(t6: T6, text: str, leg: dict[str, Any]) -> list[dict]:
    return [_approved_pending(t6, text, [leg])[1] for _ in range(2)]


def _commit_both(t6: T6, views: list[dict]) -> list[Any]:
    return _race(*[lambda v=v: t6.commit(v["authorization_id"]) for v in views])


def test_racing_bundles_cannot_both_spend_one_balance(t6: T6) -> None:
    t6.sql("UPDATE accounts SET balance_cents = 60000 WHERE account_id = ?", (JOINT,))
    leg = {"amount": "400.00", "amount_words": "400", "target_id": "payee-mum",
           "target_words": "Mum", "source_id": JOINT, "source_words": "joint"}  # fmt: skip
    views = _two_pending(t6, "Send 400 to Mum from the joint account", leg)
    results = _commit_both(t6, views)
    committed = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, ExecutionRefused)]
    assert len(committed) == 1 and len(refused) == 1, results
    assert (refused[0].status, refused[0].reason_codes) == ("BLOCKED", ("INSUFFICIENT_FUNDS",))
    assert t6.balance(JOINT) == "200.00" and t6.count("transactions") == 1


def test_racing_bundles_cannot_both_fit_the_daily_limit(t6: T6) -> None:
    t6.add_history("2600.00", T0 - timedelta(hours=1))
    leg = {"amount": "300.00", "amount_words": "300", "target_id": "payee-mum",
           "target_words": "Mum"}  # fmt: skip
    views = _two_pending(t6, "Send 300 to Mum from my savings", leg)
    results = _commit_both(t6, views)
    refused = [r for r in results if isinstance(r, ExecutionRefused)]
    assert len(refused) == 1, results
    assert refused[0].reason_codes == ("MANDATE_DAILY_LIMIT",)
    assert t6.q("SELECT SUM(amount_cents) FROM transactions")[0][0] == 290_000


def test_racing_http_approvals_cannot_overspend(t6: T6) -> None:
    t6.sql("UPDATE accounts SET balance_cents = 60000 WHERE account_id = ?", (JOINT,))
    leg = {"amount": "400.00", "amount_words": "400", "target_id": "payee-mum",
           "target_words": "Mum", "source_id": JOINT, "source_words": "joint"}  # fmt: skip
    views = [
        t6.issued(t6.bundle("Send 400 to Mum from the joint account", [leg])[1]) for _ in range(2)
    ]
    responses = _race(*[lambda v=v: t6.approve(v) for v in views])
    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["BLOCKED", "COMMITTED"]
    assert t6.balance(JOINT) == "200.00" and t6.count("transactions") == 1


# ── Velocity across bundles (injected clock) ───────────────────────


def test_velocity_blocks_sixth_transfer_leg_until_the_window_passes(t6: T6) -> None:
    two_a = [
        {"amount": "10.00", "amount_words": "10", "target_id": "payee-mum", "target_words": "Mum"},
        {"amount": "11.00", "amount_words": "11", "target_id": "payee-ahma",
         "target_words": "Ah Ma"},
    ]  # fmt: skip
    two_b = [
        {"amount": "12.00", "amount_words": "12", "target_id": "payee-lim", "target_words": "Lim"},
        {"amount": "13.00", "amount_words": "13", "target_id": "payee-priya",
         "target_type": "saved_paynow", "target_words": "Priya"},
    ]  # fmt: skip
    one = [
        {"amount": "14.00", "amount_words": "14", "target_id": "payee-mum", "target_words": "Mum"}
    ]
    assert t6.pay("Send 10 to Mum and 11 to Ah Ma from savings", two_a)["status"] == "COMMITTED"
    t6.clock.advance(minutes=1)
    assert t6.pay("Send 12 to Lim and 13 to Priya from savings", two_b)["status"] == "COMMITTED"
    t6.clock.advance(minutes=1)
    assert t6.pay("Send 14 to Mum from savings", one)["status"] == "COMMITTED"  # 5 legs at T0..T0+2

    sixth = [{"amount": "15.00", "amount_words": "15", "target_id": "payee-ahma",
              "target_words": "Ah Ma"}]  # fmt: skip
    _, bundle_id = t6.bundle("Send 15 to Ah Ma from savings", sixth)
    t6.clock.set(T0 + timedelta(minutes=9, seconds=59))  # first two legs still in the window
    resp = t6.issue(bundle_id)
    assert (resp.status_code, resp.json()["reason_codes"]) == (409, ["VELOCITY_LIMIT"])

    t6.clock.set(T0 + timedelta(minutes=11))  # the first bundle's two legs have aged out
    view = t6.issued(bundle_id)
    assert t6.approve(view).json()["status"] == "COMMITTED"
    assert t6.q("SELECT COUNT(*) FROM transactions WHERE action = 'transfer'")[0][0] == 6


def test_racing_bundles_cannot_both_take_the_last_velocity_slot(t6: T6) -> None:
    for minutes in (4, 3, 2, 1):
        t6.add_history("1.00", T0 - timedelta(minutes=minutes), action="transfer")
    views = _two_pending(t6, MUM_TEXT, MUM)
    results = _commit_both(t6, views)
    refused = [r for r in results if isinstance(r, ExecutionRefused)]
    assert len(refused) == 1 and refused[0].reason_codes == ("VELOCITY_LIMIT",)
    assert t6.q("SELECT COUNT(*) FROM transactions WHERE authorization_id IS NOT NULL")[0][0] == 1
