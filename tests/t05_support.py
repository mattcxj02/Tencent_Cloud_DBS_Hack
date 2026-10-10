"""Shared fixtures and helpers for the T-05 draft and approval tests.

Everything goes through real HTTP routes, the real delegation-token issuer and a
software WebAuthn authenticator. The only substitutions are test-only: the user
session dependency (``app.dependency_overrides``) and the eligibility hooks
(``gateway.approval.hooks._set_hook``), neither of which is reachable over HTTP.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.approval.hooks import PolicyOutcome, Receipt, _reset_hooks, _set_hook
from gateway.auth.delegation import DelegationIssuer, _set_delegation_issuer
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore
from gateway.draft_routes import _set_draft_store
from gateway.drafts import DraftStore
from gateway.identity import IdentityStore, _set_identity_store
from tests.webauthn_soft import (
    SoftCredential,
    create_registration_response,
    generate_credential,
)

TEST_SECRET = "test-secret-key-0123456789abcdefghijklmnop"
ORIGIN = "http://localhost:5173"
RP_ID = "localhost"
H = {"Origin": ORIGIN}


@dataclass
class Env:
    db_path: str
    client: TestClient
    identity: IdentityStore
    drafts: DraftStore
    issuer: DelegationIssuer
    commits: list[dict[str, Any]] = field(default_factory=list)

    def as_user(self, user_id: str) -> None:
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id=user_id)

    def allow_validation(self, policy: str = "PASS", reason: str = "") -> None:
        """Pretend T-10 validated the bundle and T-06 policy returned *policy*."""
        _set_hook("semantic_validation_passed", lambda conn, bundle: True)
        _set_hook("policy_precheck", lambda conn, ctx: PolicyOutcome(policy, reason))

    def record_commits(self) -> None:
        def _commit(conn, authz):
            self.commits.append(authz)
            return Receipt(authz["authorization_id"], "rcpt-test", "COMMITTED")

        _set_hook("commit_authorization", _commit)

    def status(self, authz_id: str) -> str:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM authorizations WHERE authorization_id = ?", (authz_id,)
            ).fetchone()
        return row[0]

    def sql(self, statement: str, params: tuple = ()) -> None:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(statement, params)


@pytest.fixture()
def env() -> Iterator[Env]:
    tmp = Path(tempfile.mkdtemp(prefix="dcta_t05_"))
    db_path = str(tmp / "dcta.db")
    bank = BankStore(db_path)
    bank.init_and_seed()
    _set_bank_store(bank)
    _set_conversation_store(ConversationStore(db_path, audio_dir=str(tmp / "audio") + "/"))
    identity = IdentityStore(db_path)
    _set_identity_store(identity)
    drafts = DraftStore(db_path)
    _set_draft_store(drafts)
    issuer = DelegationIssuer(secret=TEST_SECRET)
    _set_delegation_issuer(issuer)
    _reset_hooks()
    e = Env(db_path, TestClient(app), identity, drafts, issuer)
    e.as_user("user-matthew")
    yield e
    _reset_hooks()
    app.dependency_overrides.clear()
    for reset in (
        _set_bank_store,
        _set_conversation_store,
        _set_identity_store,
        _set_draft_store,
        _set_delegation_issuer,
    ):
        reset(None)
    shutil.rmtree(tmp, ignore_errors=True)


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def enroll(e: Env, user_id: str = "user-matthew") -> SoftCredential:
    code = e.identity.create_enrollment_code(user_id)
    resp = e.client.post("/api/auth/enroll/begin", json={"code": code}, headers=H)
    assert resp.status_code == 200, resp.text
    cred = generate_credential()
    reg = create_registration_response(
        cred,
        challenge=b64url_decode(resp.json()["challenge"]),
        rp_id=RP_ID,
        origin=ORIGIN,
        user_id=user_id,
    )
    resp = e.client.post("/api/auth/enroll/verify", json={"credential": reg}, headers=H)
    assert resp.status_code == 200, resp.text
    return cred


def new_conversation(e: Env) -> str:
    return e.client.post("/api/conversations", json={}).json()["conversation_id"]


def say(e: Env, conv_id: str, text: str) -> dict[str, Any]:
    resp = e.client.post(f"/api/conversations/{conv_id}/text", json={"text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


def revision(e: Env, conv_id: str) -> int:
    return e.client.get(f"/api/conversations/{conv_id}").json()["revision"]


def cite(turn: dict[str, Any], phrase: str, occurrence: int = 0) -> dict[str, Any]:
    start = -1
    for _ in range(occurrence + 1):
        start = turn["text"].index(phrase, start + 1)
    return {"turn_id": turn["turn_id"], "start": start, "end": start + len(phrase), "text": phrase}


def leg(
    turn: dict[str, Any],
    *,
    amount: str,
    amount_words: str,
    target_id: str,
    target_words: str,
    source_words: str,
    source_id: str = "acct-matthew-savings",
    action: str = "transfer",
    target_type: str = "saved_payee",
    leg_id: str = "leg-1",
    **extra: Any,
) -> dict[str, Any]:
    body = {
        "leg_id": leg_id,
        "action": action,
        "source_account_id": source_id,
        "target_type": target_type,
        "target_id": target_id,
        "amount_expr": {"kind": "literal", "amount": amount},
        "evidence": {
            "amount": cite(turn, amount_words),
            "target": cite(turn, target_words),
            "source": cite(turn, source_words),
        },
    }
    body.update(extra)
    return body


def freeze(e: Env, conv_id: str, legs: list[dict[str, Any]], *, rev: int | None = None):
    token = e.issuer.issue_token("user-matthew", ["draft:freeze"], conversation_id=conv_id)
    return e.client.post(
        "/internal/drafts",
        json={
            "conversation_id": conv_id,
            "conversation_revision": rev if rev is not None else revision(e, conv_id),
            "legs": legs,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def simple_bundle(e: Env) -> tuple[str, str]:
    """Freeze "Send 50 to Mum from my savings"; return (conversation_id, bundle_id)."""
    conv = new_conversation(e)
    turn = say(e, conv, "Send 50 to Mum from my savings")
    resp = freeze(
        e,
        conv,
        [
            leg(
                turn,
                amount="50.00",
                amount_words="50",
                target_id="payee-mum",
                target_words="Mum",
                source_words="savings",
            )
        ],
    )
    assert resp.status_code == 200, resp.text
    return conv, resp.json()["bundle_id"]


def authorize(e: Env, bundle_id: str, cred: SoftCredential):
    return e.client.post(
        f"/api/drafts/{bundle_id}/authorizations",
        json={"credential_id": cred.credential_id_b64},
        headers=H,
    )
