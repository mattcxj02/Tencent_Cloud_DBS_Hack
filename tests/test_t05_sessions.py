"""Tests for T-05: WebAuthn login and server-side sessions.

Covers:
- Login after enrollment → session cookie set.
- Authenticated route access with valid session.
- Route without session → 401.
- Expired/invalid session → 401.
- Logout destroys session.
- Rate limiting on login/begin.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.approval.hooks import _reset_hooks
from gateway.auth.session import require_user_session
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore
from gateway.draft_routes import _set_draft_store
from gateway.drafts import DraftStore
from gateway.identity import IdentityStore, _set_identity_store
from tests.webauthn_soft import (
    create_authentication_response,
    create_registration_response,
    generate_credential,
)

TEST_ORIGIN = "http://localhost:5173"
TEST_RP_ID = "localhost"


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    Path(db_path).unlink(missing_ok=True)

    bank_store = BankStore(db_path)
    bank_store.init_and_seed()

    audio_dir = tempfile.mkdtemp(prefix="dcta_audio_")
    conv_store = ConversationStore(db_path, audio_dir=audio_dir + "/")
    _set_bank_store(bank_store)
    _set_conversation_store(conv_store)

    identity_store = IdentityStore(db_path)
    _set_identity_store(identity_store)

    draft_store = DraftStore(db_path)
    _set_draft_store(draft_store)

    _reset_hooks()
    app.dependency_overrides.clear()

    yield db_path, bank_store, identity_store

    Path(db_path).unlink(missing_ok=True)
    import shutil

    shutil.rmtree(audio_dir, ignore_errors=True)
    _set_bank_store(None)
    _set_conversation_store(None)
    _set_identity_store(None)
    _set_draft_store(None)
    _reset_hooks()
    app.dependency_overrides.clear()


@pytest.fixture()
def client(temp_db):
    return TestClient(app)


@pytest.fixture()
def enrolled_credential(temp_db, client):
    """Enroll a software credential and return it."""
    _, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")

    resp = client.post(
        "/api/auth/enroll/begin",
        json={"code": code},
        headers={"Origin": TEST_ORIGIN},
    )
    assert resp.status_code == 200
    options = resp.json()

    challenge_b64 = options["challenge"]
    padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
    challenge = base64.urlsafe_b64decode(padded)

    soft_cred = generate_credential()
    reg_resp = create_registration_response(
        soft_cred,
        challenge=challenge,
        rp_id=TEST_RP_ID,
        origin=TEST_ORIGIN,
        user_id="user-matthew",
    )

    resp = client.post(
        "/api/auth/enroll/verify",
        json={"credential": reg_resp},
        headers={"Origin": TEST_ORIGIN},
    )
    assert resp.status_code == 200
    return soft_cred


class TestLogin:
    def test_login_sets_cookie(self, client, enrolled_credential):
        # Begin login.
        resp = client.post("/api/auth/login/begin")
        assert resp.status_code == 200
        options = resp.json()

        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        auth_resp = create_authentication_response(
            enrolled_credential,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["user_id"] == "user-matthew"
        # Cookie should be set.
        cookies = resp.headers.get("set-cookie", "")
        assert "dcta_session=" in cookies
        assert "HttpOnly" in cookies
        assert "strict" in cookies.lower()

    def test_login_wrong_origin(self, client, enrolled_credential):
        resp = client.post("/api/auth/login/begin")
        options = resp.json()

        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        auth_resp = create_authentication_response(
            enrolled_credential,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    def test_login_wrong_challenge(self, client, enrolled_credential):
        import secrets

        wrong_challenge = secrets.token_bytes(32)
        auth_resp = create_authentication_response(
            enrolled_credential,
            challenge=wrong_challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 400

    def test_login_unknown_credential(self, client, enrolled_credential):
        resp = client.post("/api/auth/login/begin")
        options = resp.json()

        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        # Generate a different credential that's not enrolled.
        unknown_cred = generate_credential()
        auth_resp = create_authentication_response(
            unknown_cred,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 401


class TestSession:
    def test_valid_session_accesses_routes(self, client, enrolled_credential):
        # Login.
        resp = client.post("/api/auth/login/begin")
        options = resp.json()
        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        auth_resp = create_authentication_response(
            enrolled_credential,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200
        session_cookie = resp.cookies.get("dcta_session")
        assert session_cookie

        # Use the session cookie to access a user route.
        resp = client.post(
            "/api/conversations",
            json={},
            cookies={"dcta_session": session_cookie},
        )
        assert resp.status_code == 200

    def test_no_session_401(self, client):
        # Clear overrides to test real session dependency.
        app.dependency_overrides.pop(require_user_session, None)
        resp = client.post("/api/conversations", json={})
        assert resp.status_code == 401

    def test_invalid_session_401(self, client):
        resp = client.post(
            "/api/conversations",
            json={},
            cookies={"dcta_session": "invalid-token"},
        )
        assert resp.status_code == 401

    def test_logout_destroys_session(self, client, enrolled_credential):
        # Login.
        resp = client.post("/api/auth/login/begin")
        options = resp.json()
        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        auth_resp = create_authentication_response(
            enrolled_credential,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
        )

        resp = client.post(
            "/api/auth/login/verify",
            json={"assertion": auth_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        session_cookie = resp.cookies.get("dcta_session")

        # Logout.
        resp = client.post(
            "/api/auth/logout",
            cookies={"dcta_session": session_cookie},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200

        # Session should be invalid now.
        resp = client.post(
            "/api/conversations",
            json={},
            cookies={"dcta_session": session_cookie},
        )
        assert resp.status_code == 401


class TestRateLimit:
    def test_rate_limit_login_begin(self, client):
        """Exceeding the login/begin rate limit returns 429."""
        # RATE_LIMIT_MAX = 10, so 10 should pass and 11th should fail.
        for i in range(10):
            resp = client.post("/api/auth/login/begin")
            assert resp.status_code == 200, f"request {i} failed"

        resp = client.post("/api/auth/login/begin")
        assert resp.status_code == 429
