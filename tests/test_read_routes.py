"""Read routes for the web UI: ``/api/auth/me``, ``/api/accounts`` and the evidence view.

Owner only, session required, nothing secret. The evidence canary test plants values
that must never leave the gateway (an audit payload secret, the step-up code, assertion
material, the delegation token) and checks none of them appears in the view.
"""

from __future__ import annotations

import json

import pytest

from gateway.app import app
from gateway.audit import append_audit
from gateway.auth.session import require_user_session
from ledger.schema import connect
from tests.orch_support import MUM, MUM_TEXT, USER, Orch, o_fixture  # noqa: F401
from tests.t05_support import H
from tests.t07_support import OP, OPERATOR_TOKEN, standalone
from tests.t10_support import v_fixture  # noqa: F401
from tests.webauthn_soft import create_authentication_response

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


# ── /api/auth/me ─────────────────────────────────────────────────────


def test_me_returns_user_display_name_and_credentials(o: Orch) -> None:
    resp = o.client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {
        "user_id": USER,
        "display_name": "Matthew",
        "credentials": [
            {
                "credential_id": o.v.cred.credential_id_b64,
                "created_at": o.v.q(
                    "SELECT created_at FROM webauthn_credentials WHERE credential_id = ?",
                    (o.v.cred.credential_id_b64,),
                )[0][0],
            }
        ],
    }
    assert "public_key" not in resp.text


def test_me_for_another_user_shows_only_theirs(o: Orch) -> None:
    o.env.as_user("user-ana")
    body = o.client.get("/api/auth/me").json()
    assert (body["user_id"], body["display_name"], body["credentials"]) == ("user-ana", "Ana", [])


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/accounts", "/api/cards"])
def test_read_routes_need_a_session(o: Orch, path: str) -> None:
    app.dependency_overrides.pop(require_user_session)
    assert o.client.get(path).status_code == 401


# ── /api/accounts ────────────────────────────────────────────────────


def test_accounts_are_masked_with_balances(o: Orch) -> None:
    resp = o.client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == [
        {"account_id": "acct-joint-01", "display": "Joint ••9012", "balance": "2000.00"},
        {"account_id": "acct-matthew-current", "display": "Current ••5678", "balance": "3500.00"},
        {"account_id": "acct-matthew-savings", "display": "Savings ••1234", "balance": "12000.00"},
    ]


def test_accounts_are_scoped_to_the_session_user(o: Orch) -> None:
    o.env.as_user("user-ana")
    ids = [a["account_id"] for a in o.client.get("/api/accounts").json()]
    assert ids == ["acct-ana-savings", "acct-joint-01"]


def test_cards_show_outstanding_balance_for_the_session_user_only(o: Orch) -> None:
    resp = o.client.get("/api/cards")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == [
        {"card_id": "card-matthew-01", "display": "Card ••4242", "outstanding": "642.30"}
    ]
    o.env.as_user("user-ana")
    assert [c["card_id"] for c in o.client.get("/api/cards").json()] == ["card-ana-01"]


# ── evidence ─────────────────────────────────────────────────────────


def test_evidence_is_owner_only(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    assert o.evidence(conv).status_code == 200
    o.env.as_user("user-ana")
    assert o.evidence(conv).status_code == 404
    assert o.evidence("conv-unknown").status_code == 404


def test_evidence_shape_after_a_correction_and_a_ready_draft(o: Orch) -> None:
    conv = o.conversation("Send fifty bucks to Mum from my current")
    first = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
    edit = o.client.post(
        f"/api/conversations/{conv}/transcript-edit",
        json={"edits_turn_id": first["turn_id"], "text": MUM_TEXT},
    )
    assert edit.status_code == 200, edit.text
    o.extractor(MUM)
    ready = o.drafted(conv)
    assert ready["status"] == "ready"

    view = o.evidence(conv).json()
    assert set(view) == {
        "conversation_id",
        "revision",
        "turns",
        "drafts",
        "authorizations",
        "audit",
        "trace_urls",
    }
    assert (view["conversation_id"], view["revision"]) == (conv, 2)
    original, corrected = view["turns"]
    assert (original["superseded"], corrected["superseded"]) == (True, False)
    assert corrected["edits_turn_id"] == original["turn_id"]
    assert set(original) == {
        "turn_id",
        "seq",
        "role",
        "text",
        "edits_turn_id",
        "answers_question_id",
        "created_at",
        "superseded",
    }
    [draft] = view["drafts"]
    assert (draft["bundle_id"], draft["bundle_hash"], draft["status"]) == (
        ready["bundle_id"],
        ready["bundle_hash"],
        "DRAFT_FROZEN",
    )
    assert draft["legs"] == ready["legs"]
    assert set(draft["validation"]) == {
        "verdict",
        "status",
        "issues",
        "model",
        "prompt_hash",
        "trace_url",
    }
    assert (draft["validation"]["verdict"], draft["validation"]["trace_url"]) == ("MATCH", None)
    assert view["authorizations"] == []
    assert view["audit"]["ok"] is True and view["audit"]["first_bad_seq"] is None
    event = view["audit"]["events"][0]
    assert set(event) == {"seq", "ts", "source", "step", "trace_id"}
    steps = [e["step"] for e in view["audit"]["events"]]
    assert "gateway.draft.freeze" in steps and "gateway.validation.match" in steps
    assert {"agent.extract", "agent.propose"} <= set(steps)
    assert view["trace_urls"] == []  # telemetry is disabled in this test


def test_evidence_reports_a_broken_audit_chain(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    seq = o.v.q("SELECT MIN(seq) FROM audit_events WHERE conversation_id = ?", (conv,))[0][0]
    o.v.sql("UPDATE audit_events SET step = 'gateway.tampered' WHERE seq = ?", (seq,))
    audit = o.evidence(conv).json()["audit"]
    assert (audit["ok"], audit["first_bad_seq"]) == (False, seq)


def test_evidence_never_contains_secrets(o: Orch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    canary = "CANARY-otp-7c1e9d"
    conv = o.conversation("Send 600 bucks to Mum from my savings")
    conn = connect(o.v.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        append_audit(
            conn,
            source="gateway",
            conversation_id=conv,
            user_id=USER,
            step="gateway.test.secret_payload",
            payload={"otp": canary, "authorization": f"Bearer {canary}"},
        )
        conn.commit()
    finally:
        conn.close()

    from tests.agent_support import intent, reply

    o.extractor(reply(intent("transfer", "600 bucks", "Mum", "savings")))
    ready = o.drafted(conv)
    assert ready["status"] == "ready"
    token = o.agent_requests[-1].headers["authorization"].removeprefix("Bearer ")
    view = o.v.issue(ready["bundle_id"]).json()
    approved = o.v.approve(view)
    assert approved.json()["status"] == "AWAITING_STEP_UP", approved.text
    code = o.client.post(
        f"/operator/step-up/{view['authorization_id']}/deliver", headers=OP
    ).json()["code"]
    submitted = o.client.post(
        f"/api/authorizations/{view['authorization_id']}/step-up",
        json={"code": code},
        headers=H,
    )
    assert submitted.json()["status"] == "COMMITTED", submitted.text

    evidence = o.evidence(conv)
    text = evidence.text
    body = evidence.json()
    [authz] = body["authorizations"]
    assert authz["status"] == "COMMITTED"
    assert authz["approval"]["user_verified"] is True
    assert authz["step_up"] == {"status": "CONSUMED", "attempts": 0}
    assert authz["receipt"]["receipt_id"] == submitted.json()["receipt_id"]

    challenge = o.v.q(
        "SELECT salt, verifier FROM step_up_challenges WHERE authorization_id = ?",
        (view["authorization_id"],),
    )[0]
    evidence_row = o.v.q(
        "SELECT client_data_hash, authenticator_data_hash FROM approval_evidence "
        "WHERE authorization_id = ?",
        (view["authorization_id"],),
    )[0]
    nonce = json.loads(
        o.v.q(
            "SELECT envelope_bytes FROM authorizations WHERE authorization_id = ?",
            (view["authorization_id"],),
        )[0][0]
    )["nonce"]
    assertion = create_authentication_response(
        o.v.cred,
        challenge=b"\x00" * 32,
        rp_id="localhost",
        origin="http://localhost:5173",
        sign_count=0,
    )
    forbidden = {
        "audit payload canary": canary,
        "delegation token": token,
        "step-up salt": challenge["salt"],
        "step-up verifier": challenge["verifier"],
        "client data hash": evidence_row["client_data_hash"],
        "authenticator data hash": evidence_row["authenticator_data_hash"],
        "envelope nonce": nonce,
        "signature field": '"signature"',
        "clientDataJSON": "clientDataJSON",
        "public key": "public_key",
        "payload_json": "payload_json",
        "user handle": assertion["response"].get("userHandle") or "userHandle",
    }
    for label, value in forbidden.items():
        assert value not in text, f"{label} leaked into the evidence view"
    assert not standalone(code, text), "step-up code leaked into the evidence view"
