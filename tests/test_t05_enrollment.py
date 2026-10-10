"""Tests for T-05: WebAuthn enrollment flow.

Covers:
- Enrollment code generation, consumption, single-use, expiry.
- Registration options issuance.
- Registration verification and credential storage.
- Wrong/expired/used enrollment codes rejected.
- Origin check on mutating routes.
- Cross-user: enrollment code bound to its user.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.approval.hooks import _reset_hooks
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore
from gateway.draft_routes import _set_draft_store
from gateway.drafts import DraftStore
from gateway.identity import IdentityStore
from tests.webauthn_soft import create_registration_response, generate_credential

TEST_ORIGIN = "http://localhost:5173"
TEST_RP_ID = "localhost"


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def temp_db():
    """Create a temp DB, init+seed, wire stores and app."""
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
    from gateway.identity import _set_identity_store

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


# ── Tests ───────────────────────────────────────────────────────────


class TestEnrollmentCode:
    def test_create_and_consume(self, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        assert len(code) > 16
        user_id = identity.consume_enrollment_code(code)
        assert user_id == "user-matthew"

    def test_wrong_code_rejected(self, temp_db):
        _, _, identity = temp_db
        with pytest.raises(ValueError, match="invalid enrollment code"):
            identity.consume_enrollment_code("totally-wrong-code")

    def test_single_use(self, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        identity.consume_enrollment_code(code)
        with pytest.raises(ValueError, match="already used"):
            identity.consume_enrollment_code(code)

    def test_expired_code_rejected(self, temp_db):
        _, _, identity = temp_db
        from gateway.identity import ENROLLMENT_EXPIRY_MINUTES

        code = identity.create_enrollment_code("user-matthew")
        # Manually expire the code by updating its expires_at.
        from datetime import UTC, datetime, timedelta

        from shared.timeutil import to_iso

        past = to_iso(datetime.now(tz=UTC) - timedelta(minutes=ENROLLMENT_EXPIRY_MINUTES + 1))
        from ledger.schema import connect

        conn = connect(temp_db[0])
        conn.execute(
            "UPDATE enrollment_codes SET expires_at = ? WHERE code_hash = ?",
            (past, hashlib_sha256(code)),
        )
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="expired"):
            identity.consume_enrollment_code(code)


def hashlib_sha256(val: str) -> str:
    import hashlib

    return hashlib.sha256(val.encode("utf-8")).hexdigest()


class TestEnrollBegin:
    def test_enroll_begin_returns_options(self, client, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "challenge" in data
        assert data["rp"]["id"] == TEST_RP_ID
        assert data["user"]["name"] == "user-matthew"

    def test_enroll_begin_wrong_code(self, client):
        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": "wrong"},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 400

    def test_enroll_begin_wrong_origin(self, client, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403


class TestEnrollVerify:
    def test_full_enrollment(self, client, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")

        # Begin enrollment.
        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200
        options = resp.json()

        # Decode challenge from options.
        import base64

        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        # Generate a software credential.
        soft_cred = generate_credential()

        # Create registration response.
        reg_resp = create_registration_response(
            soft_cred,
            challenge=challenge,
            rp_id=TEST_RP_ID,
            origin=TEST_ORIGIN,
            user_id="user-matthew",
        )

        # Submit verification.
        resp = client.post(
            "/api/auth/enroll/verify",
            json={"credential": reg_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["user_id"] == "user-matthew"
        assert data["status"] == "enrolled"

        # Verify credential is stored.
        stored = identity.get_credential(soft_cred.credential_id_b64)
        assert stored is not None
        assert stored["user_id"] == "user-matthew"

    def test_enroll_verify_wrong_origin(self, client, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")

        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": TEST_ORIGIN},
        )
        options = resp.json()
        import base64

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
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    def test_enroll_verify_wrong_rp(self, client, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")

        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": TEST_ORIGIN},
        )
        options = resp.json()
        import base64

        challenge_b64 = options["challenge"]
        padded = challenge_b64 + "=" * (4 - len(challenge_b64) % 4)
        challenge = base64.urlsafe_b64decode(padded)

        soft_cred = generate_credential()
        # Use wrong RP ID.
        reg_resp = create_registration_response(
            soft_cred,
            challenge=challenge,
            rp_id="evil.com",
            origin=TEST_ORIGIN,
            user_id="user-matthew",
        )

        resp = client.post(
            "/api/auth/enroll/verify",
            json={"credential": reg_resp},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 400

    def test_code_bound_to_user(self, client, temp_db):
        """Enrollment code for user-matthew cannot enroll for user-ana."""
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")

        resp = client.post(
            "/api/auth/enroll/begin",
            json={"code": code},
            headers={"Origin": TEST_ORIGIN},
        )
        assert resp.status_code == 200
        # The options should be for user-matthew, not user-ana.
        assert resp.json()["user"]["name"] == "user-matthew"
