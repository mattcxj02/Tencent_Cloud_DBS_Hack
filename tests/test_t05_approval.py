"""T-05 exact-payload approval: issuance fails closed, approval is bound and single-use.

Assertions are produced by a software authenticator over the challenge the server
handed out, exactly as a browser would. A verified signature alone never commits.
"""

from __future__ import annotations

import secrets
import threading

import pytest

from shared.canonical import approval_challenge
from shared.schemas import SigningEnvelope
from tests.t05_support import (
    ORIGIN,
    RP_ID,
    Env,
    H,
    authorize,
    b64url_decode,
    enroll,
    say,
    simple_bundle,
)
from tests.webauthn_soft import create_authentication_response


def _issue(e: Env, *, policy: str = "PASS"):
    """Enroll, freeze, allow validation and issue an authorization."""
    cred = enroll(e)
    conv, bundle_id = simple_bundle(e)
    e.allow_validation(policy)
    resp = authorize(e, bundle_id, cred)
    assert resp.status_code == 200, resp.text
    return cred, conv, resp.json()


def _assert(cred, view, **kwargs):
    challenge = b64url_decode(view["webauthn_options"]["challenge"])
    return create_authentication_response(
        cred,
        challenge=kwargs.pop("challenge", challenge),
        rp_id=kwargs.pop("rp_id", RP_ID),
        origin=kwargs.pop("origin", ORIGIN),
        **kwargs,
    )


def _approve(e: Env, view, assertion, *, expected_hash=None, headers=H, **extra):
    return e.client.post(
        f"/api/authorizations/{view['authorization_id']}/approve",
        json={
            "authorization_id": view["authorization_id"],
            "expected_envelope_hash": expected_hash or view["envelope_hash"],
            "assertion": assertion,
            **extra,
        },
        headers=headers,
    )


# ── Issuance ────────────────────────────────────────────────────────


def test_issuance_fails_closed_without_validation_or_policy(env: Env) -> None:
    cred = enroll(env)
    _, bundle_id = simple_bundle(env)
    resp = authorize(env, bundle_id, cred)
    assert (resp.status_code, resp.json()["detail"]) == (409, "SEMANTIC_VALIDATION_REQUIRED")

    from gateway.approval.hooks import PolicyOutcome, _reset_hooks, _set_hook

    _set_hook("semantic_validation_passed", lambda conn, bundle: True)
    _set_hook("policy_precheck", lambda conn, ctx: PolicyOutcome("BLOCK", "TEST"))
    resp = authorize(env, bundle_id, cred)  # a policy BLOCK refuses issuance (T-06)
    assert resp.status_code == 409 and resp.json()["detail"].startswith("POLICY_BLOCK")
    assert resp.json()["reason_codes"] == ["TEST"]

    _reset_hooks()  # back to the defaults: semantic validation fails closed until T-10
    resp = authorize(env, bundle_id, cred)
    assert (resp.status_code, resp.json()["detail"]) == (409, "SEMANTIC_VALIDATION_REQUIRED")


def test_envelope_is_exactly_what_is_signed(env: Env) -> None:
    _, _, view = _issue(env)
    envelope = SigningEnvelope.model_validate(view["envelope"])
    assert envelope.operation == "execute_bundle"
    assert envelope.operation_id == envelope.payload.bundle_id.__str__()
    assert envelope.payload.legs[0].display_metadata.display_name == "Mum"
    assert b64url_decode(view["webauthn_options"]["challenge"]) == approval_challenge(envelope)
    assert view["webauthn_options"]["userVerification"] == "required"
    again = env.client.get(f"/api/authorizations/{view['authorization_id']}").json()
    assert again["envelope"] == view["envelope"]


def test_cannot_issue_with_someone_elses_credential(env: Env) -> None:
    env.as_user("user-ana")
    ana_cred = enroll(env, "user-ana")
    env.as_user("user-matthew")
    _, bundle_id = simple_bundle(env)
    env.allow_validation()
    resp = authorize(env, bundle_id, ana_cred)
    assert resp.json()["detail"] == "CREDENTIAL_NOT_OWNED"


def test_client_cannot_choose_operation(env: Env) -> None:
    cred = enroll(env)
    _, bundle_id = simple_bundle(env)
    env.allow_validation()
    resp = env.client.post(
        f"/api/drafts/{bundle_id}/authorizations",
        json={"credential_id": cred.credential_id_b64, "operation": "update_mandate"},
        headers=H,
    )
    assert resp.status_code == 422


# ── Approval: success path ──────────────────────────────────────────


def test_valid_approval_reaches_commit_once(env: Env) -> None:
    cred, _, view = _issue(env)
    env.record_commits()
    resp = _approve(env, view, _assert(cred, view))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "COMMITTED"
    assert [c["authorization_id"] for c in env.commits] == [view["authorization_id"]]


def test_without_execution_returns_503_and_assertion_is_single_use(env: Env) -> None:
    from gateway.approval.hooks import _set_hook

    def _unavailable(conn, authz):
        raise NotImplementedError

    cred, _, view = _issue(env)
    _set_hook("commit_authorization", _unavailable)  # execution unavailable
    assertion = _assert(cred, view)
    resp = _approve(env, view, assertion)
    assert (resp.status_code, resp.json()["detail"]) == (503, "COMMIT_NOT_IMPLEMENTED")
    assert env.status(view["authorization_id"]) == "PENDING_APPROVAL"
    replay = _approve(env, view, _assert(cred, view))
    assert replay.json()["detail"] == "ALREADY_APPROVED"


def test_step_up_and_block_never_commit(env: Env) -> None:
    for policy, expected in (("STEP_UP", "AWAITING_STEP_UP"), ("BLOCK", "BLOCKED")):
        cred, _, view = _issue(env)
        env.record_commits()
        env.allow_validation(policy, "TEST")  # policy changes after issuance
        resp = _approve(env, view, _assert(cred, view))
        assert resp.json()["status"] == expected
        assert env.status(view["authorization_id"]) == expected
        assert env.commits == []


def test_concurrent_approvals_commit_at_most_once(env: Env) -> None:
    cred, _, view = _issue(env)
    env.record_commits()
    assertions = [_assert(cred, view, sign_count=0) for _ in range(4)]
    results: list[int] = []

    def worker(a):
        results.append(_approve(env, view, a).status_code)

    threads = [threading.Thread(target=worker, args=(a,)) for a in assertions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(200) == 1
    assert len(env.commits) == 1


# ── Approval: rejections leave no commit ────────────────────────────


@pytest.mark.parametrize(
    "tamper",
    [
        {"origin": "https://evil.example"},
        {"rp_id": "evil.example"},
        {"flags": 0x01},  # user present but not verified
        {"challenge": secrets.token_bytes(32)},
    ],
    ids=["wrong-origin", "wrong-rp", "no-user-verification", "wrong-challenge"],
)
def test_bad_assertion_rejected_and_does_not_cancel(env: Env, tamper: dict) -> None:
    cred, _, view = _issue(env)
    env.record_commits()
    resp = _approve(env, view, _assert(cred, view, **tamper))
    assert (resp.status_code, resp.json()["detail"]) == (409, "SIGNATURE_INVALID")
    assert env.status(view["authorization_id"]) == "PENDING_APPROVAL"
    assert env.commits == []
    # The genuine approval still works afterwards.
    assert _approve(env, view, _assert(cred, view)).json()["status"] == "COMMITTED"


def test_other_user_cannot_see_or_approve(env: Env) -> None:
    cred, _, view = _issue(env)
    env.as_user("user-ana")
    assert env.client.get(f"/api/authorizations/{view['authorization_id']}").status_code == 404
    assert _approve(env, view, _assert(cred, view)).status_code == 404
    env.as_user("user-matthew")
    assert env.status(view["authorization_id"]) == "PENDING_APPROVAL"


def test_tampered_hash_extra_fields_and_missing_origin(env: Env) -> None:
    cred, _, view = _issue(env)
    resp = _approve(env, view, _assert(cred, view), expected_hash="0" * 64)
    assert resp.json()["detail"] == "ENVELOPE_HASH_MISMATCH"
    resp = _approve(env, view, _assert(cred, view), payload={"amount": "1.00"})
    assert resp.status_code == 422
    resp = _approve(env, view, _assert(cred, view), headers={})
    assert resp.status_code == 403


def test_expired_authorization(env: Env) -> None:
    cred, _, view = _issue(env)
    env.sql(
        "UPDATE authorizations SET expires_at = '2020-01-01T00:00:00.000Z' "
        "WHERE authorization_id = ?",
        (view["authorization_id"],),
    )
    resp = _approve(env, view, _assert(cred, view))
    assert resp.json()["detail"] == "AUTHORIZATION_EXPIRED"
    assert env.status(view["authorization_id"]) == "EXPIRED"


def test_correction_supersedes_pending_approval(env: Env) -> None:
    cred, conv, view = _issue(env)
    say(env, conv, "Wait, make it 15")
    assert env.status(view["authorization_id"]) == "SUPERSEDED"
    resp = _approve(env, view, _assert(cred, view))
    assert resp.json()["detail"] == "NOT_PENDING: SUPERSEDED"


def test_mandate_change_supersedes_on_approval(env: Env) -> None:
    cred, _, view = _issue(env)
    env.sql(
        "INSERT INTO mandates (mandate_id, user_id, version, status, per_leg_cents, "
        "daily_cents, allowed_actions, effective_at, created_at) "
        "SELECT mandate_id || '-v2', user_id, version + 1, 'active', per_leg_cents, "
        "daily_cents, allowed_actions, effective_at, created_at FROM mandates "
        "WHERE user_id = 'user-matthew' AND status = 'active'"
    )
    resp = _approve(env, view, _assert(cred, view))
    assert resp.json()["detail"] == "AUTHORIZATION_SUPERSEDED"
    assert env.status(view["authorization_id"]) == "SUPERSEDED"


def test_mandate_operation_cannot_be_approved_on_bundle_route(env: Env) -> None:
    cred, _, view = _issue(env)
    env.sql(
        "UPDATE authorizations SET operation = 'update_mandate' WHERE authorization_id = ?",
        (view["authorization_id"],),
    )
    resp = _approve(env, view, _assert(cred, view))
    assert resp.json()["detail"] == "OPERATION_MISMATCH"
