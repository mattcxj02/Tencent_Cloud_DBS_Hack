"""T-07 signed mandate management: cooling, immediate decreases/pause, rules, binding.

Real routes, real passkey verification (software authenticator), real policy
and a frozen gateway clock.  Seed: Matthew's mandate v1 is 1,000.00 per leg,
3,000.00 per day, all four actions, active, expiring 2027-12-31.
"""

from __future__ import annotations

import base64
import threading
from datetime import timedelta
from typing import Any

import pytest

from gateway.audit import verify_chain
from gateway.execution import ExecutionRefused
from gateway.mandates import MandateService, evaluate_change
from gateway.policy import evaluate as payment_policy
from ledger.schema import connect
from shared.canonical import approval_challenge
from shared.schemas import SigningEnvelope
from shared.timeutil import from_iso, to_iso
from tests.t05_support import H
from tests.t06_support import SAVINGS, T0, T6, t6_fixture  # noqa: F401  (registers "t6")
from tests.t07_support import (
    BIG,
    BIG_TEXT,
    SMALL,
    SMALL_TEXT,
    approve_change,
    awaiting,
    change,
    mandate,
    request_change,
)
from tests.webauthn_soft import create_authentication_response

ALL_ACTIONS = ["transfer", "pay_bill", "pay_card", "buy_equity"]


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _issue_payment(t6: T6, amount: str, words: str | None = None):
    text = f"Send {words or amount} to Mum from my savings"
    leg = {"amount": amount, "amount_words": words or amount, "target_id": "payee-mum",
           "target_words": "Mum"}  # fmt: skip
    return t6.issue(t6.bundle(text, [leg])[1])


def _versions(t6: T6) -> list[tuple]:
    return t6.q(
        "SELECT version, status, per_leg_cents, daily_cents, allowed_actions FROM mandates "
        "WHERE user_id = 'user-matthew' ORDER BY version"
    )


# ── The envelope ───────────────────────────────────────────────────


def test_change_envelope_is_update_mandate_without_conversation(t6: T6) -> None:
    resp = request_change(t6, per_leg_limit="800.00")
    assert resp.status_code == 200, resp.text
    view = resp.json()
    envelope = view["envelope"]
    assert envelope["operation"] == "update_mandate"
    assert envelope["active_mandate_version"] == 1
    assert envelope["user_id"] == "user-matthew"
    assert envelope["credential_id"] == t6.cred.credential_id_b64
    for field in ("conversation_id", "conversation_revision", "conversation_hash"):
        assert envelope[field] is None
    # The signed payload is the full target setting, computed by the gateway.
    assert envelope["payload"] == {
        "per_leg_limit": "800.00",
        "daily_limit": "3000.00",
        "allowed_actions": ALL_ACTIONS,
        "paused": False,
    }
    ttl = from_iso(envelope["expires_at"]) - from_iso(envelope["issued_at"])
    assert ttl == timedelta(seconds=120)
    parsed = SigningEnvelope.model_validate(envelope)
    assert _b64url(view["webauthn_options"]["challenge"]) == approval_challenge(parsed)
    assert view["envelope_hash"] == approval_challenge(parsed).hex()
    assert view["change"]["direction"] == "tighten" and view["change"]["applies"] == "immediately"
    assert "does not lock" in view["scope_note"]
    # Viewing it again returns the same stored envelope.
    again = t6.env.client.get(f"/api/mandate/changes/{view['authorization_id']}").json()
    assert again["envelope"] == envelope and again["envelope_hash"] == view["envelope_hash"]
    assert _versions(t6) == [(1, "active", 100000, 300000, ",".join(ALL_ACTIONS))]


def test_change_request_needs_origin_and_own_credential(t6: T6) -> None:
    no_origin = t6.env.client.post(
        "/api/mandate/changes",
        json={"credential_id": t6.cred.credential_id_b64, "paused": True},
    )
    assert no_origin.status_code == 403
    t6.env.as_user("user-ana")
    resp = request_change(t6, paused=True)  # Matthew's credential
    assert (resp.status_code, resp.json()["detail"]) == (409, "CREDENTIAL_NOT_OWNED")
    assert t6.count("authorizations") == 0


# ── Cooling ────────────────────────────────────────────────────────


def test_increase_cools_for_12_hours_while_the_old_version_stays_active(t6: T6) -> None:
    receipt = change(t6, per_leg_limit="1500.00")["receipt"]
    assert receipt["outcome"] == "PENDING_COOLING" and receipt["new_version"] is None
    assert receipt["effective_at"] == to_iso(T0 + timedelta(hours=12))

    view = mandate(t6)
    assert view["current"]["version"] == 1 and view["current"]["per_leg_limit"] == "1000.00"
    assert view["pending"]["per_leg_limit"] == "1500.00"
    assert view["pending"]["effective_at"] == to_iso(T0 + timedelta(hours=12))
    assert view["cooling_hours"] == 12

    # The old limit still governs payments during cooling.
    blocked = _issue_payment(t6, "1200.00", "1200")
    assert blocked.json()["reason_codes"] == ["MANDATE_PER_LEG_LIMIT"]

    t6.clock.set(T0 + timedelta(hours=12) - timedelta(seconds=1))
    assert mandate(t6)["current"]["version"] == 1

    t6.clock.set(T0 + timedelta(hours=12))
    view = mandate(t6)
    assert view["current"]["version"] == 2 and view["current"]["per_leg_limit"] == "1500.00"
    assert view["pending"] is None
    assert view["history"][0]["status"] == "ACTIVATED"
    assert view["history"][0]["resulting_version"] == 2
    assert "gateway.mandate.activated" in t6.audit_steps()

    allowed = _issue_payment(t6, "1200.00", "1200")
    assert allowed.status_code == 200 and allowed.json()["envelope"]["active_mandate_version"] == 2


def test_activation_requires_fresh_approval_of_older_payments(t6: T6) -> None:
    change(t6, daily_limit="4000.00")
    t6.clock.set(T0 + timedelta(hours=12) - timedelta(seconds=30))
    _, bundle_id = t6.bundle(SMALL_TEXT, [SMALL])
    view = t6.issued(bundle_id)  # bound to v1, still inside its 120 s window
    assert view["envelope"]["active_mandate_version"] == 1

    t6.clock.set(T0 + timedelta(hours=12) + timedelta(seconds=1))
    resp = t6.approve(view)
    assert (resp.status_code, resp.json()["detail"]) == (409, "AUTHORIZATION_SUPERSEDED")
    assert resp.json()["reason_codes"] == ["MANDATE_VERSION_CHANGED"]
    assert t6.status(view["authorization_id"]) == "SUPERSEDED"
    assert t6.count("transactions") == 0
    assert mandate(t6)["current"]["daily_limit"] == "4000.00"

    fresh = t6.issued(bundle_id)  # the same draft, re-approved under v2
    assert fresh["envelope"]["active_mandate_version"] == 2
    assert t6.approve(fresh).json()["status"] == "COMMITTED"


def test_a_new_pending_increase_replaces_the_previous_one(t6: T6) -> None:
    first = change(t6, per_leg_limit="1500.00")["receipt"]
    t6.clock.advance(hours=6)
    second = change(t6, daily_limit="4000.00")["receipt"]
    assert second["cancelled_pending_changes"] == [first["operation_id"]]
    assert second["effective_at"] == to_iso(T0 + timedelta(hours=18))

    view = mandate(t6)
    assert view["pending"]["change_id"] == second["operation_id"]
    # Replacement, not merge: the pending setting is v1 plus the second request.
    assert (view["pending"]["per_leg_limit"], view["pending"]["daily_limit"]) == (
        "1000.00",
        "4000.00",
    )
    statuses = {h["change_id"]: (h["status"], h["status_reason"]) for h in view["history"]}
    assert statuses[first["operation_id"]] == ("CANCELLED", "MANDATE_CHANGE_REPLACED")

    t6.clock.set(T0 + timedelta(hours=12))  # the first one's time: nothing happens
    assert mandate(t6)["current"]["version"] == 1
    t6.clock.set(T0 + timedelta(hours=18))
    current = mandate(t6)["current"]
    assert (current["version"], current["per_leg_limit"], current["daily_limit"]) == (
        2,
        "1000.00",
        "4000.00",
    )


# ── Immediate decreases and pause ──────────────────────────────────


def test_decrease_applies_now_cancels_pending_increase_and_supersedes_payments(
    t6: T6,
) -> None:
    pending = change(t6, daily_limit="5000.00")["receipt"]
    plain = t6.issued(t6.bundle(SMALL_TEXT, [SMALL])[1])  # PENDING_APPROVAL
    stepped = awaiting(t6)  # AWAITING_STEP_UP with a challenge

    receipt = change(t6, per_leg_limit="400.00")["receipt"]
    assert receipt["outcome"] == "APPLIED" and receipt["new_version"] == 2
    assert receipt["effective_at"] == to_iso(T0)
    assert receipt["cancelled_pending_changes"] == [pending["operation_id"]]
    assert sorted(receipt["superseded_authorizations"]) == sorted(
        [plain["authorization_id"], stepped["authorization_id"]]
    )

    view = mandate(t6)
    assert view["current"]["version"] == 2
    assert (view["current"]["per_leg_limit"], view["current"]["daily_limit"]) == (
        "400.00",
        "3000.00",
    )
    assert view["pending"] is None
    for authz_id in (plain["authorization_id"], stepped["authorization_id"]):
        assert t6.status(authz_id) == "SUPERSEDED"
    assert t6.q(
        "SELECT status FROM step_up_challenges WHERE authorization_id = ?",
        (stepped["authorization_id"],),
    ) == [("SUPERSEDED",)]
    assert t6.approve(plain).json()["detail"] == "NOT_PENDING: SUPERSEDED"
    assert _issue_payment(t6, "450.00", "450").json()["reason_codes"] == ["MANDATE_PER_LEG_LIMIT"]
    assert t6.count("transactions") == 0


def test_pause_is_immediate_and_unpause_cools(t6: T6) -> None:
    receipt = change(t6, paused=True)["receipt"]
    assert receipt["outcome"] == "APPLIED" and "does not lock" in receipt["scope_note"]
    view = mandate(t6)
    assert view["current"]["status"] == "paused" and view["current"]["paused"] is True
    assert "other banking channel" in view["scope_note"]

    blocked = _issue_payment(t6, "50.00", "50")
    assert (blocked.status_code, blocked.json()["detail"]) == (409, "NO_ACTIVE_MANDATE")

    unpause = change(t6, paused=False)["receipt"]
    assert unpause["outcome"] == "PENDING_COOLING"
    t6.clock.advance(hours=11)
    assert mandate(t6)["current"]["status"] == "paused"
    t6.clock.set(T0 + timedelta(hours=12))
    assert mandate(t6)["current"]["status"] == "active"
    assert _issue_payment(t6, "50.00", "50").status_code == 200


def test_paused_or_expired_mandate_can_still_be_managed(t6: T6) -> None:
    change(t6, paused=True)
    tighter = change(t6, per_leg_limit="300.00")["receipt"]  # still paused, lower limit
    assert tighter["outcome"] == "APPLIED"
    assert _versions(t6)[-1][:3] == (3, "paused", 30000)

    t6.sql("UPDATE mandates SET expires_at = '2026-01-01T00:00:00.000Z' WHERE user_id = ?",
           ("user-matthew",))  # fmt: skip
    assert mandate(t6)["current"]["expired"] is True
    receipt = change(t6, daily_limit="1000.00")["receipt"]
    assert receipt["outcome"] == "APPLIED"
    assert t6.q(
        "SELECT expires_at FROM mandates WHERE user_id = 'user-matthew' AND version = 4"
    ) == [("2026-01-01T00:00:00.000Z",)]
    relax = change(t6, paused=False)["receipt"]  # relaxation still cools
    assert relax["outcome"] == "PENDING_COOLING"


def test_operation_specific_policy_ignores_payment_eligibility(t6: T6) -> None:
    # No funds, expired KYC and a velocity history do not stop a settings change.
    t6.sql("UPDATE accounts SET balance_cents = 0")
    t6.sql("UPDATE kyc SET status = 'expired'")
    view = request_change(t6, per_leg_limit="100.00").json()
    envelope = SigningEnvelope.model_validate(view["envelope"])
    conn = connect(t6.db)
    try:
        assert evaluate_change(conn, envelope, T0).decision == "PASS"
        # The payment policy never accepts a mandate envelope (and vice versa).
        assert payment_policy(conn, envelope, T0).reason_codes == ("UNSUPPORTED_OPERATION",)
    finally:
        conn.close()
    assert approve_change(t6, view).json()["status"] == "COMMITTED"


# ── Change rules ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"per_leg_limit": "500.00", "daily_limit": "4000.00"}, "MANDATE_CHANGE_MIXED"),
        ({"paused": True, "daily_limit": "3500.00"}, "MANDATE_CHANGE_MIXED"),
        (
            {"allowed_actions": ["transfer", "pay_bill"], "per_leg_limit": "1200.00"},
            "MANDATE_CHANGE_MIXED",
        ),
        ({}, "MANDATE_NO_CHANGE"),
        ({"per_leg_limit": "1000.00", "paused": False}, "MANDATE_NO_CHANGE"),
        ({"per_leg_limit": "0.00"}, "MANDATE_LIMIT_INVALID"),
        ({"per_leg_limit": "12.345"}, "MANDATE_LIMIT_INVALID"),
        ({"daily_limit": "-5"}, "MANDATE_LIMIT_INVALID"),
        ({"daily_limit": "20000.01"}, "MANDATE_LIMIT_ABOVE_MAXIMUM"),
        ({"daily_limit": "500.00"}, "MANDATE_PER_LEG_ABOVE_DAILY"),
        ({"allowed_actions": ["transfer", "transfer"]}, "MANDATE_ACTIONS_DUPLICATE"),
    ],
)
def test_change_rules_reject_without_an_envelope(t6: T6, fields: dict[str, Any], code: str) -> None:
    resp = request_change(t6, **fields)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == code
    if code == "MANDATE_CHANGE_MIXED":
        assert "separate" in resp.json()["message"]
    assert t6.count("authorizations") == 0


def test_floats_and_unknown_fields_are_rejected(t6: T6) -> None:
    assert request_change(t6, per_leg_limit=1500.0).status_code == 422
    assert request_change(t6, paused="true").status_code == 422
    assert request_change(t6, operation="execute_bundle", paused=True).status_code == 422
    assert request_change(t6, allowed_actions=["withdraw_cash"]).status_code == 422
    assert t6.count("authorizations") == 0


def test_removing_and_adding_actions(t6: T6) -> None:
    removed = change(t6, allowed_actions=["transfer", "pay_bill", "pay_card"])["receipt"]
    assert removed["direction"] == "tighten" and removed["new_version"] == 2
    _, bundle_id = t6.bundle(
        "Buy 300 of DBS with my savings",
        [{"amount": "300.00", "amount_words": "300", "target_id": "D05",
          "target_words": "DBS", "action": "buy_equity", "target_type": "sgx_symbol",
          "equity_terms": {"quote_id": t6.quote("D05")["quote_id"]}}],
    )  # fmt: skip
    resp = t6.issue(bundle_id)
    assert resp.json()["reason_codes"] == ["MANDATE_ACTION_NOT_ALLOWED"]
    added = change(t6, allowed_actions=ALL_ACTIONS)["receipt"]
    assert added["direction"] == "loosen" and added["outcome"] == "PENDING_COOLING"


# ── Binding between operations ─────────────────────────────────────


def test_mandate_change_cannot_use_the_bundle_route_and_vice_versa(t6: T6) -> None:
    change_view = request_change(t6, per_leg_limit="900.00").json()
    on_bundle_route = t6.approve(change_view)  # /api/authorizations/{id}/approve
    assert (on_bundle_route.status_code, on_bundle_route.json()["detail"]) == (
        409,
        "OPERATION_MISMATCH",
    )
    assert t6.status(change_view["authorization_id"]) == "PENDING_APPROVAL"

    payment_view = t6.issued(t6.bundle(SMALL_TEXT, [SMALL])[1])
    on_mandate_route = approve_change(t6, payment_view)
    assert (on_mandate_route.status_code, on_mandate_route.json()["detail"]) == (
        409,
        "OPERATION_MISMATCH",
    )
    shown = t6.env.client.get(f"/api/mandate/changes/{payment_view['authorization_id']}")
    assert shown.status_code == 404
    assert t6.status(payment_view["authorization_id"]) == "PENDING_APPROVAL"
    assert _versions(t6)[-1][0] == 1 and t6.count("transactions") == 0

    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(change_view["authorization_id"])
    assert refused.value.code == "OPERATION_MISMATCH"


def test_a_mandate_signature_cannot_authorize_a_payment(t6: T6) -> None:
    change_view = request_change(t6, per_leg_limit="900.00").json()
    payment_view = t6.issued(t6.bundle(SMALL_TEXT, [SMALL])[1])
    mandate_assertion = t6.assertion(change_view)  # signs the mandate challenge
    resp = t6.env.client.post(
        f"/api/authorizations/{payment_view['authorization_id']}/approve",
        json={
            "authorization_id": payment_view["authorization_id"],
            "expected_envelope_hash": payment_view["envelope_hash"],
            "assertion": mandate_assertion,
        },
        headers=H,
    )
    assert (resp.status_code, resp.json()["detail"]) == (409, "SIGNATURE_INVALID")
    assert t6.status(payment_view["authorization_id"]) == "PENDING_APPROVAL"
    assert t6.count("transactions") == 0

    # ...and a payment signature cannot approve the mandate change.
    payment_assertion = t6.assertion(payment_view)
    resp = t6.env.client.post(
        f"/api/mandate/changes/{change_view['authorization_id']}/approve",
        json={
            "authorization_id": change_view["authorization_id"],
            "expected_envelope_hash": change_view["envelope_hash"],
            "assertion": payment_assertion,
        },
        headers=H,
    )
    assert (resp.status_code, resp.json()["detail"]) == (409, "SIGNATURE_INVALID")
    assert _versions(t6)[-1][0] == 1


def test_wrong_owner_gets_404(t6: T6) -> None:
    view = request_change(t6, per_leg_limit="900.00").json()
    t6.env.as_user("user-ana")
    assert t6.env.client.get(f"/api/mandate/changes/{view['authorization_id']}").status_code == 404
    resp = approve_change(t6, view)
    assert (resp.status_code, resp.json()["detail"]) == (404, "AUTHORIZATION_NOT_FOUND")
    ana = mandate(t6)
    assert ana["current"]["version"] == 1 and ana["pending"] is None
    t6.env.as_user("user-matthew")
    assert t6.status(view["authorization_id"]) == "PENDING_APPROVAL"


# ── Approval checks ────────────────────────────────────────────────


def test_bad_assertions_change_nothing(t6: T6) -> None:
    view = request_change(t6, paused=True).json()
    wrong_origin = t6.env.client.post(
        f"/api/mandate/changes/{view['authorization_id']}/approve",
        json={
            "authorization_id": view["authorization_id"],
            "expected_envelope_hash": view["envelope_hash"],
            "assertion": create_authentication_response(
                t6.cred,
                challenge=_b64url(view["webauthn_options"]["challenge"]),
                rp_id="localhost",
                origin="http://evil.example",
                sign_count=0,
            ),
        },
        headers=H,
    )
    assert wrong_origin.json()["detail"] == "SIGNATURE_INVALID"
    no_uv = approve_change(t6, view, flags=0x01)
    assert no_uv.json()["detail"] == "SIGNATURE_INVALID"
    wrong_hash = t6.env.client.post(
        f"/api/mandate/changes/{view['authorization_id']}/approve",
        json={
            "authorization_id": view["authorization_id"],
            "expected_envelope_hash": "0" * 64,
            "assertion": t6.assertion(view),
        },
        headers=H,
    )
    assert wrong_hash.json()["detail"] == "ENVELOPE_HASH_MISMATCH"
    assert t6.status(view["authorization_id"]) == "PENDING_APPROVAL"
    assert _versions(t6) == [(1, "active", 100000, 300000, ",".join(ALL_ACTIONS))]
    assert "gateway.approval.signature_rejected" in t6.audit_steps()


def test_expired_change_envelope_cannot_be_approved(t6: T6) -> None:
    view = request_change(t6, paused=True).json()
    t6.clock.advance(seconds=120)
    resp = approve_change(t6, view)
    assert (resp.status_code, resp.json()["detail"]) == (409, "AUTHORIZATION_EXPIRED")
    assert t6.status(view["authorization_id"]) == "EXPIRED"
    assert mandate(t6)["current"]["status"] == "active"


def test_change_bound_to_an_old_version_is_superseded(t6: T6) -> None:
    first = request_change(t6, per_leg_limit="900.00").json()
    second = request_change(t6, daily_limit="2500.00").json()
    assert approve_change(t6, first).json()["status"] == "COMMITTED"
    assert t6.status(second["authorization_id"]) == "SUPERSEDED"
    resp = approve_change(t6, second)
    assert resp.json()["detail"] == "NOT_PENDING: SUPERSEDED"
    assert mandate(t6)["current"]["daily_limit"] == "3000.00"


def test_approval_is_single_use_and_idempotent(t6: T6) -> None:
    view = request_change(t6, per_leg_limit="900.00").json()
    first = approve_change(t6, view).json()
    second = approve_change(t6, view).json()
    assert second["replayed"] is True and second["receipt_id"] == first["receipt_id"]
    assert len(_versions(t6)) == 2
    receipt = t6.env.client.get(f"/api/authorizations/{view['authorization_id']}/receipt")
    assert receipt.json()["receipt"]["operation"] == "update_mandate"


def test_concurrent_approvals_apply_once(t6: T6) -> None:
    view = request_change(t6, per_leg_limit="900.00").json()
    assertions = [t6.assertion(view) for _ in range(3)]
    service = MandateService(t6.db)
    barrier = threading.Barrier(3)
    results: list[Any] = [None] * 3

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = service.approve_change(
                user_id="user-matthew",
                authz_id=view["authorization_id"],
                expected_envelope_hash=view["envelope_hash"],
                assertion=assertions[i],
            )
        except Exception as exc:  # collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(isinstance(r, dict) and r["status"] == "COMMITTED" for r in results), results
    assert sorted(r["replayed"] for r in results) == [False, True, True]
    assert len(_versions(t6)) == 2 and t6.count("mandate_changes") == 1


def test_mandate_changes_are_audited_on_an_intact_chain(t6: T6) -> None:
    change(t6, per_leg_limit="1500.00")
    change(t6, paused=True)
    steps = t6.audit_steps()
    for step in (
        "gateway.mandate.change_requested",
        "gateway.approval.signature_verified",
        "gateway.mandate.change_cancelled",
        "gateway.mandate.change_committed",
    ):
        assert step in steps
    conn = connect(t6.db)
    try:
        assert verify_chain(conn)["ok"]
    finally:
        conn.close()


def test_step_up_payment_needs_reapproval_after_pause_and_cooling(t6: T6) -> None:
    """End to end: pause supersedes a step-up payment; after unpause cools, it works again."""
    view = awaiting(t6)
    change(t6, paused=True)
    assert t6.status(view["authorization_id"]) == "SUPERSEDED"
    change(t6, paused=False)
    t6.clock.set(T0 + timedelta(hours=12))
    _, bundle_id = t6.bundle(BIG_TEXT, [BIG])
    fresh = t6.issued(bundle_id)
    assert fresh["envelope"]["active_mandate_version"] == 3
    assert t6.approve(fresh).json()["status"] == "AWAITING_STEP_UP"
    assert t6.balance(SAVINGS) == "12000.00"
