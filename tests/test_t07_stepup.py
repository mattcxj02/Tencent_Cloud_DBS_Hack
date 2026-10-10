"""T-07 simulated out-of-band step-up: binding, single use, limits and atomicity.

Every test drives the real routes, the real policy and the real commit on a
temporary database with a frozen gateway clock (``tests.t06_support``).  The
code reaches the test only through the operator console route, the same way
the demo presenter reads it.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.audit import verify_chain
from gateway.execution import ExecutionRefused
from gateway.stepup import SIMULATION_LABEL, StepUpService
from ledger.schema import connect
from shared.timeutil import from_iso
from tests.t05_support import H
from tests.t06_support import SAVINGS, T6, t6_fixture  # noqa: F401  (registers "t6")
from tests.t07_support import (
    BIG,
    BIG_TEXT,
    OP,
    OPERATOR_TOKEN,
    SMALL,
    SMALL_TEXT,
    awaiting,
    challenge,
    change,
    deliver,
    operator_fixture,  # noqa: F401  (registers "operator")
    request_change,
    standalone,
    submit,
    wrong,
)

# ── Happy path ─────────────────────────────────────────────────────


def test_in_limit_step_up_commits_exactly_once(t6: T6, operator: TestClient) -> None:
    before = t6.balance(SAVINGS)
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    assert t6.count("transactions") == 0 and t6.balance(SAVINGS) == before

    status = t6.env.client.get(f"/api/authorizations/{authz_id}/step-up").json()
    assert status["status"] == "PENDING" and status["attempts_remaining"] == 5
    assert status["channel"] == SIMULATION_LABEL and "code" not in status
    # The challenge expires with the signed envelope (<= 120 s after issue).
    assert status["expires_at"] == view["expires_at"]

    delivered = deliver(operator, authz_id)
    assert delivered["simulation"] == SIMULATION_LABEL
    assert len(delivered["code"]) == 6 and delivered["code"].isdigit()

    resp = submit(t6, authz_id, delivered["code"], expected_envelope_hash=view["envelope_hash"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "COMMITTED" and body["replayed"] is False
    assert body["receipt"]["step_up_verified"] is True
    assert t6.balance(SAVINGS) == "11400.00" and t6.count("transactions") == 1
    assert t6.status(authz_id) == "COMMITTED"
    assert challenge(t6, authz_id)["status"] == "CONSUMED"

    # Replay after success: the stored receipt, no second payment, proof not reused.
    again = submit(t6, authz_id, delivered["code"])
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["receipt_id"] == body["receipt_id"]
    assert t6.balance(SAVINGS) == "11400.00" and t6.count("transactions") == 1
    assert challenge(t6, authz_id)["status"] == "CONSUMED"

    steps = t6.audit_steps()
    for step in (
        "gateway.authorization.awaiting_step_up",
        "gateway.stepup.issued",
        "gateway.stepup.delivered",
        "gateway.stepup.verified",
        "gateway.execution.commit",
    ):
        assert step in steps
    conn = connect(t6.db)
    try:
        assert verify_chain(conn)["ok"]
    finally:
        conn.close()


def test_step_up_needs_session_origin(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    code = deliver(operator, view["authorization_id"])["code"]
    resp = t6.env.client.post(
        f"/api/authorizations/{view['authorization_id']}/step-up", json={"code": code}
    )
    assert resp.status_code == 403
    assert challenge(t6, view["authorization_id"])["failed"] == 0


# ── Wrong code / user / authorization / hash ───────────────────────


def test_wrong_codes_count_and_the_fifth_blocks(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]

    for attempt, guess in enumerate([wrong(code), "12ab56", "", wrong(wrong(code))], start=1):
        if guess == "":
            guess = "0" * 7  # wrong length is still an attempt
        resp = submit(t6, authz_id, guess)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "STEP_UP_CODE_INVALID"
        assert resp.json()["attempts_remaining"] == 5 - attempt
        assert t6.status(authz_id) == "AWAITING_STEP_UP"

    fifth = submit(t6, authz_id, wrong(code))
    assert fifth.status_code == 200
    assert fifth.json()["status"] == "BLOCKED"
    assert fifth.json()["reason_codes"] == ["STEP_UP_ATTEMPTS_EXHAUSTED"]
    assert t6.status(authz_id) == "BLOCKED"
    assert challenge(t6, authz_id) | {"expires_at": None} == {
        "status": "BLOCKED",
        "failed": 5,
        "deliveries": 1,
        "expires_at": None,
        "reason": "STEP_UP_ATTEMPTS_EXHAUSTED",
    }

    # The right code no longer helps, and the channel will not deliver again.
    late = submit(t6, authz_id, code)
    assert (late.status_code, late.json()["detail"]) == (409, "NOT_PENDING: BLOCKED")
    resend = operator.post(f"/operator/step-up/{authz_id}/deliver", headers=OP)
    assert (resend.status_code, resend.json()["detail"]) == (409, "STEP_UP_NOT_ACTIVE")
    assert t6.count("transactions") == 0


def test_another_user_cannot_submit_or_see_the_challenge(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]

    t6.env.as_user("user-ana")
    assert submit(t6, authz_id, code).status_code == 404
    assert t6.env.client.get(f"/api/authorizations/{authz_id}/step-up").status_code == 404
    resend = t6.env.client.post(f"/api/authorizations/{authz_id}/step-up/resend", headers=H)
    assert resend.status_code == 404
    assert challenge(t6, authz_id)["failed"] == 0 and t6.count("transactions") == 0

    t6.env.as_user("user-matthew")
    assert submit(t6, authz_id, code).json()["status"] == "COMMITTED"


def test_code_for_another_authorization_is_rejected(t6: T6, operator: TestClient) -> None:
    first = awaiting(t6)
    second = awaiting(t6)
    code_1 = deliver(operator, first["authorization_id"])["code"]
    code_2 = deliver(operator, second["authorization_id"])["code"]
    if code_1 == code_2:  # one in a million: the codes are independent
        pytest.skip("independent codes collided")

    resp = submit(t6, second["authorization_id"], code_1)
    assert resp.json()["detail"] == "STEP_UP_CODE_INVALID"
    assert challenge(t6, second["authorization_id"])["failed"] == 1

    assert submit(t6, second["authorization_id"], code_2).json()["status"] == "COMMITTED"
    assert t6.status(first["authorization_id"]) == "AWAITING_STEP_UP"
    assert t6.count("transactions") == 1


def test_wrong_envelope_hash_and_broken_binding(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]

    resp = submit(t6, authz_id, code, expected_envelope_hash="0" * 64)
    assert (resp.status_code, resp.json()["detail"]) == (409, "ENVELOPE_HASH_MISMATCH")
    assert challenge(t6, authz_id)["failed"] == 0

    # A challenge row re-bound to another envelope never verifies; it blocks.
    t6.sql(
        "UPDATE step_up_challenges SET envelope_hash = ? WHERE authorization_id = ?",
        ("f" * 64, authz_id),
    )
    blocked = submit(t6, authz_id, code)
    assert blocked.json()["status"] == "BLOCKED"
    assert blocked.json()["reason_codes"] == ["STEP_UP_BINDING_MISMATCH"]
    assert t6.status(authz_id) == "BLOCKED" and t6.count("transactions") == 0


def test_step_up_is_refused_when_not_awaiting(t6: T6) -> None:
    _, bundle_id = t6.bundle(SMALL_TEXT, [SMALL])
    view = t6.issued(bundle_id)  # PENDING_APPROVAL, no passkey yet
    resp = submit(t6, view["authorization_id"], "123456")
    assert (resp.status_code, resp.json()["detail"]) == (409, "STEP_UP_NOT_REQUIRED")
    assert t6.status(view["authorization_id"]) == "PENDING_APPROVAL"
    assert t6.count("step_up_challenges") == 0


# ── Expiry and resend ──────────────────────────────────────────────


def test_expired_challenge_cannot_commit(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    issued = from_iso(view["envelope"]["issued_at"])
    assert (from_iso(challenge(t6, authz_id)["expires_at"]) - issued).total_seconds() <= 120

    t6.clock.set(from_iso(view["expires_at"]))
    reapprove = t6.approve(view)  # no longer "pending"
    assert (reapprove.status_code, reapprove.json()["detail"]) == (409, "AUTHORIZATION_EXPIRED")
    resp = submit(t6, authz_id, code)
    assert (resp.status_code, resp.json()["detail"]) == (409, "AUTHORIZATION_EXPIRED")
    assert t6.status(authz_id) == "EXPIRED"
    assert challenge(t6, authz_id)["status"] == "EXPIRED"
    assert t6.count("transactions") == 0
    late = operator.post(f"/operator/step-up/{authz_id}/deliver", headers=OP)
    assert late.status_code == 409


def test_resend_never_extends_expiry(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    first = deliver(operator, authz_id)
    t6.clock.advance(seconds=30)
    second = deliver(operator, authz_id)
    assert second["code"] == first["code"]
    assert second["expires_at"] == first["expires_at"] == view["expires_at"]
    assert (first["deliveries"], second["deliveries"]) == (1, 2)

    resend = t6.env.client.post(f"/api/authorizations/{authz_id}/step-up/resend", headers=H)
    assert resend.status_code == 200 and resend.json()["expires_at"] == view["expires_at"]
    assert "code" not in resend.json()

    t6.clock.set(from_iso(view["expires_at"]))
    expired = t6.env.client.post(f"/api/authorizations/{authz_id}/step-up/resend", headers=H)
    assert (expired.status_code, expired.json()["detail"]) == (409, "STEP_UP_EXPIRED")
    assert submit(t6, authz_id, first["code"]).json()["detail"] == "AUTHORIZATION_EXPIRED"


def test_repeated_approval_returns_pending_without_a_new_code(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]

    again = t6.approve(view)  # a fresh, valid passkey assertion
    assert again.status_code == 200
    assert again.json()["status"] == "AWAITING_STEP_UP"
    assert again.json()["reason_codes"] == ["RISK_THRESHOLD_EXCEEDED"]
    assert t6.count("step_up_challenges") == 1
    assert t6.audit_steps().count("gateway.stepup.issued") == 1
    assert deliver(operator, authz_id)["code"] == code
    assert t6.count("transactions") == 0


# ── Hard limits still win ──────────────────────────────────────────


def test_balance_drop_blocks_even_with_a_valid_code(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    t6.sql("UPDATE accounts SET balance_cents = 59999 WHERE account_id = ?", (SAVINGS,))

    resp = submit(t6, authz_id, code)
    assert resp.status_code == 200
    assert resp.json()["status"] == "BLOCKED"
    assert resp.json()["reason_codes"] == ["INSUFFICIENT_FUNDS"]
    assert t6.status(authz_id) == "BLOCKED"
    # The valid code was used up with the blocked outcome; it cannot be replayed later.
    assert challenge(t6, authz_id)["status"] == "CONSUMED"
    t6.sql("UPDATE accounts SET balance_cents = 1200000 WHERE account_id = ?", (SAVINGS,))
    late = submit(t6, authz_id, code)
    assert late.json()["detail"] == "NOT_PENDING: BLOCKED"
    assert t6.count("transactions") == 0 and t6.count("receipts") == 0


def test_kyc_expiry_blocks_even_with_a_valid_code(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    code = deliver(operator, view["authorization_id"])["code"]
    t6.sql("UPDATE kyc SET status = 'expired' WHERE user_id = 'user-matthew'")
    resp = submit(t6, view["authorization_id"], code)
    assert resp.json()["status"] == "BLOCKED"
    assert resp.json()["reason_codes"] == ["KYC_EXPIRED"]
    assert t6.count("transactions") == 0


# ── Invalidation ───────────────────────────────────────────────────


def test_mandate_pause_invalidates_a_pending_step_up(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]

    receipt = change(t6, paused=True)["receipt"]
    assert receipt["superseded_authorizations"] == [authz_id]
    assert t6.status(authz_id) == "SUPERSEDED"
    assert challenge(t6, authz_id)["status"] == "SUPERSEDED"
    resp = submit(t6, authz_id, code)
    assert resp.json()["detail"] == "NOT_PENDING: SUPERSEDED"
    assert t6.count("transactions") == 0


def test_conversation_correction_supersedes_the_challenge(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    conv_id = view["envelope"]["conversation_id"]

    resp = t6.env.client.post(
        f"/api/conversations/{conv_id}/text", json={"text": "Actually make it 60"}
    )
    assert resp.status_code == 200, resp.text
    assert t6.status(authz_id) == "SUPERSEDED"
    assert challenge(t6, authz_id)["status"] == "SUPERSEDED"
    assert submit(t6, authz_id, code).json()["detail"] == "NOT_PENDING: SUPERSEDED"
    assert t6.count("transactions") == 0


def test_cancel_closes_the_challenge(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    resp = t6.env.client.post(f"/api/authorizations/{authz_id}/cancel", headers=H)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
    assert challenge(t6, authz_id)["status"] == "CANCELLED"
    assert submit(t6, authz_id, code).json()["detail"] == "NOT_PENDING: CANCELLED"
    again = t6.env.client.post(f"/api/authorizations/{authz_id}/cancel", headers=H)
    assert (again.status_code, again.json()["detail"]) == (409, "NOT_PENDING: CANCELLED")


def test_commit_failure_after_a_valid_code_invalidates_it(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    t6.sql(
        "CREATE TRIGGER fail_settlement BEFORE INSERT ON transactions "
        "BEGIN SELECT RAISE(ABORT, 'settlement unavailable'); END"
    )
    resp = submit(t6, authz_id, code)
    assert (resp.status_code, resp.json()["detail"]) == (503, "COMMIT_FAILED")
    assert t6.count("transactions") == 0 and t6.balance(SAVINGS) == "12000.00"
    # Fail closed: the rolled-back consumption is replaced by an explicit invalidation.
    assert challenge(t6, authz_id)["status"] == "INVALIDATED"
    assert challenge(t6, authz_id)["reason"] == "COMMIT_FAILED"

    t6.sql("DROP TRIGGER fail_settlement")
    retry = submit(t6, authz_id, code)
    assert (retry.status_code, retry.json()["detail"]) == (409, "STEP_UP_NOT_ACTIVE")
    assert t6.count("transactions") == 0


# ── Concurrency ────────────────────────────────────────────────────


def test_concurrent_submissions_of_one_code_commit_once(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    service = StepUpService(t6.db)
    barrier = threading.Barrier(4)
    results: list[Any] = [None] * 4

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = service.submit(user_id="user-matthew", authz_id=authz_id, code=code)
        except Exception as exc:  # collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert all(isinstance(r, dict) and r["status"] == "COMMITTED" for r in results), results
    assert sorted(r["replayed"] for r in results) == [False, True, True, True]
    assert len({r["receipt_id"] for r in results}) == 1
    assert t6.count("transactions") == 1 and t6.balance(SAVINGS) == "11400.00"
    assert t6.audit_steps().count("gateway.stepup.verified") == 1


def test_a_bare_commit_cannot_skip_the_proof(t6: T6) -> None:
    view = awaiting(t6)
    with pytest.raises(ExecutionRefused) as refused:
        t6.commit(view["authorization_id"])
    assert refused.value.code == "STEP_UP_REQUIRED"
    assert t6.count("transactions") == 0


# ── Operator channel isolation and secrecy ─────────────────────────


def test_operator_route_needs_its_own_credential(t6: T6, monkeypatch: pytest.MonkeyPatch) -> None:
    view = awaiting(t6)
    url = f"/operator/step-up/{view['authorization_id']}/deliver"
    console = TestClient(t6.env.client.app)

    monkeypatch.setenv("OPERATOR_TOKEN", "")
    disabled = console.post(url, headers=OP)
    assert (disabled.status_code, disabled.json()["detail"]) == (503, "OPERATOR_CHANNEL_DISABLED")
    monkeypatch.setenv("OPERATOR_TOKEN", "too-short")
    assert console.post(url, headers={"X-Operator-Token": "too-short"}).status_code == 503

    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    assert console.post(url).status_code == 401
    assert (
        console.post(url, headers={"X-Operator-Token": OPERATOR_TOKEN[:-1] + "x"}).status_code
        == 401
    )

    agent_token = t6.env.issuer.issue_token("user-matthew", ["bank:read", "draft:freeze"])
    as_agent = console.post(url, headers={"Authorization": f"Bearer {agent_token}"})
    assert as_agent.status_code == 403
    with_agent_header = console.post(url, headers={**OP, "Authorization": f"Bearer {agent_token}"})
    assert with_agent_header.status_code == 403
    assert console.post(url, headers={"X-Operator-Token": agent_token}).status_code == 401

    browser = TestClient(t6.env.client.app, cookies={"dcta_session": "any-user-session"})
    assert browser.post(url, headers=OP).status_code == 403
    assert browser.get("/operator/step-up", headers=OP).status_code == 403
    assert t6.audit_steps().count("gateway.stepup.delivered") == 0

    listed = console.get("/operator/step-up", headers=OP)
    assert listed.status_code == 200 and listed.json()["simulation"] == SIMULATION_LABEL
    [entry] = listed.json()["challenges"]
    assert entry["authorization_id"] == view["authorization_id"] and "code" not in entry
    ok = console.post(url, headers=OP)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"


def test_code_never_stored_audited_or_shown_to_the_session(t6: T6, operator: TestClient) -> None:
    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    submit(t6, authz_id, wrong(code))
    committed = submit(t6, authz_id, code).json()

    user_views = [
        committed,
        t6.env.client.get(f"/api/authorizations/{authz_id}").json(),
        t6.env.client.get(f"/api/authorizations/{authz_id}/receipt").json(),
        t6.env.client.get(f"/api/authorizations/{authz_id}/step-up").json(),
    ]
    stored = [
        *(row[0] for row in t6.q("SELECT payload_json FROM audit_events")),
        *(row[0] for row in t6.q("SELECT receipt_json FROM receipts")),
        *(json.dumps(row) for row in t6.q("SELECT * FROM step_up_challenges")),
        *(json.dumps(row) for row in t6.q("SELECT status, status_reason FROM authorizations")),
    ]
    for text in [json.dumps(v) for v in user_views] + stored:
        assert not standalone(code, text), text
        assert not standalone(wrong(code), text), text
    assert "verifier" not in json.dumps(user_views)


def test_mandate_change_authorization_has_no_step_up(t6: T6) -> None:
    view = request_change(t6, per_leg_limit="900.00").json()
    resp = submit(t6, view["authorization_id"], "123456")
    assert (resp.status_code, resp.json()["detail"]) == (409, "OPERATION_MISMATCH")


def test_step_up_payment_after_pending_small_payment(t6: T6, operator: TestClient) -> None:
    """Two independent authorizations: step-up on one never touches the other."""
    small = t6.pay(SMALL_TEXT, [SMALL])
    assert small["status"] == "COMMITTED"
    view = awaiting(t6, leg={**BIG})
    code = deliver(operator, view["authorization_id"])["code"]
    assert submit(t6, view["authorization_id"], code).json()["status"] == "COMMITTED"
    assert t6.balance(SAVINGS) == "11350.00" and t6.count("receipts") == 2


def test_agent_token_cannot_complete_or_manage(t6: T6, operator: TestClient) -> None:
    """The agent's delegation token is not a user session: no proof, cancel or mandate change."""
    from gateway.app import app
    from gateway.auth.session import require_user_session

    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    app.dependency_overrides.pop(require_user_session, None)  # real session check
    token = t6.env.issuer.issue_token(
        "user-matthew", ["bank:read", "draft:freeze", "conversation:read"]
    )
    agent = {**H, "Authorization": f"Bearer {token}"}
    client = TestClient(app)
    for method, url, body in (
        ("post", f"/api/authorizations/{authz_id}/step-up", {"code": code}),
        ("get", f"/api/authorizations/{authz_id}/step-up", None),
        ("post", f"/api/authorizations/{authz_id}/cancel", None),
        ("post", "/api/mandate/changes", {"credential_id": "x", "paused": True}),
        ("get", "/api/mandate", None),
    ):
        resp = getattr(client, method)(url, headers=agent, **({"json": body} if body else {}))
        assert resp.status_code == 401, (url, resp.text)
    assert t6.status(authz_id) == "AWAITING_STEP_UP"
    assert challenge(t6, authz_id)["status"] == "PENDING"
    assert t6.count("transactions") == 0


def test_commit_path_issues_the_challenge_atomically(t6: T6, operator: TestClient) -> None:
    """Pre-check said PASS but the authoritative recheck says STEP_UP: one challenge, no money."""
    from gateway.approval.hooks import PolicyOutcome, _set_hook

    _, bundle_id = t6.bundle(BIG_TEXT, [BIG])
    view = t6.issued(bundle_id)
    _set_hook("policy_precheck", lambda conn, ctx: PolicyOutcome("PASS"))
    resp = t6.approve(view)
    assert resp.json()["status"] == "AWAITING_STEP_UP"
    assert t6.count("step_up_challenges") == 1 and t6.count("transactions") == 0
    code = deliver(operator, view["authorization_id"])["code"]
    assert submit(t6, view["authorization_id"], code).json()["status"] == "COMMITTED"


def test_codes_from_another_secret_are_unusable(t6: T6, operator: TestClient) -> None:
    """A restart with a new per-process secret: the old challenge cannot be delivered or met."""
    from gateway.stepup import _set_secret

    view = awaiting(t6)
    authz_id = view["authorization_id"]
    code = deliver(operator, authz_id)["code"]
    _set_secret(b"another-process-secret-0123456789abcdef")
    try:
        resp = operator.post(f"/operator/step-up/{authz_id}/deliver", headers=OP)
        assert (resp.status_code, resp.json()["detail"]) == (409, "STEP_UP_CODE_UNAVAILABLE")
        assert submit(t6, authz_id, code).json()["detail"] == "STEP_UP_CODE_INVALID"
    finally:
        _set_secret(None)
    assert t6.count("transactions") == 0
