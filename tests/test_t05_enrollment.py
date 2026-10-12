"""Tests for T-05: WebAuthn enrollment flow.

Covers:
- Enrollment code generation, reservation, successful consumption, single-use, expiry.
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
    def test_create_and_begin(self, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        assert len(code) > 16
        user_id = identity.begin_enrollment(code, b"unit-challenge")
        assert user_id == "user-matthew"

    def test_wrong_code_rejected(self, temp_db):
        _, _, identity = temp_db
        with pytest.raises(ValueError, match="invalid enrollment code"):
            identity.begin_enrollment("totally-wrong-code", b"unit-challenge")

    def test_single_use(self, temp_db):
        _, _, identity = temp_db
        code = identity.create_enrollment_code("user-matthew")
        challenge = b"unit-challenge"
        identity.begin_enrollment(code, challenge)
        identity.complete_enrollment(
            challenge, credential_id="unit-credential", public_key=b"verified-synthetic-key",
            sign_count=0, transports=[], backup_eligible=False, backup_state=False,
        )
        with pytest.raises(ValueError, match="already used"):
            identity.begin_enrollment(code, b"next-challenge")

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
            identity.begin_enrollment(code, b"unit-challenge")


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


def _begin(client, code):
    response = client.post(
        "/api/auth/enroll/begin", json={"code": code}, headers={"Origin": TEST_ORIGIN}
    )
    assert response.status_code == 200, response.text
    from tests.t05_support import b64url_decode

    return b64url_decode(response.json()["challenge"])


def _registration(challenge, *, rp_id=TEST_RP_ID):
    credential = generate_credential()
    return credential, create_registration_response(
        credential, challenge=challenge, rp_id=rp_id, origin=TEST_ORIGIN,
        user_id="user-matthew",
    )


def _verify(client, registration):
    return client.post(
        "/api/auth/enroll/verify", json={"credential": registration},
        headers={"Origin": TEST_ORIGIN},
    )


def test_cancel_begin_again_success_and_siblings_replay_blocked(client, temp_db):
    _, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    cancelled = _begin(client, code)
    active = _begin(client, code)
    credential, registration = _registration(active)
    assert _verify(client, registration).status_code == 200
    assert identity.get_credential(credential.credential_id_b64) is not None
    assert _verify(client, registration).status_code == 400
    _, old_registration = _registration(cancelled)
    assert _verify(client, old_registration).status_code == 400
    assert client.post(
        "/api/auth/enroll/begin", json={"code": code}, headers={"Origin": TEST_ORIGIN}
    ).status_code == 400
    assert len(identity.get_user_credentials("user-matthew")) == 1


def test_failed_registration_does_not_consume_code(client, temp_db):
    _, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    first = _begin(client, code)
    _, invalid = _registration(first, rp_id="evil.com")
    assert _verify(client, invalid).status_code == 400
    _, valid = _registration(_begin(client, code))
    assert _verify(client, valid).status_code == 200


def test_enrollment_begins_limited_persistently(client, temp_db):
    db, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    for _ in range(5):
        _begin(client, code)
    other_store = IdentityStore(db)
    with pytest.raises(ValueError, match="begin limit"):
        other_store.begin_enrollment(code, b"sixth-challenge")
    assert client.post(
        "/api/auth/enroll/begin", json={"code": code}, headers={"Origin": TEST_ORIGIN}
    ).status_code == 400


@pytest.mark.parametrize("table", ["enrollment_codes", "auth_challenges"])
def test_expiry_after_begin_blocks_registration(client, temp_db, table):
    db, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    _, registration = _registration(_begin(client, code))
    from ledger.schema import connect

    conn = connect(db)
    conn.execute(f"UPDATE {table} SET expires_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    assert _verify(client, registration).status_code == 400
    assert identity.get_user_credentials("user-matthew") == []


@pytest.mark.parametrize("same_challenge", [False, True])
def test_concurrent_registration_has_one_success(client, temp_db, same_challenge):
    from concurrent.futures import ThreadPoolExecutor

    _, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    registrations = [_registration(_begin(client, code))[1] for _ in range(2)]
    if same_challenge:
        registrations[1] = registrations[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda r: _verify(client, r), registrations))
    assert sorted(r.status_code for r in responses) == [200, 400]
    assert len(identity.get_user_credentials("user-matthew")) == 1


def test_failed_credential_insert_rolls_back_code_and_challenges(client, temp_db):
    _, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    first = _begin(client, code)
    second = _begin(client, code)
    credential, registration = _registration(first)
    identity.store_credential(
        credential_id=credential.credential_id_b64, user_id="user-matthew",
        public_key=b"existing", sign_count=0, transports=[], backup_eligible=False,
        backup_state=False,
    )
    assert _verify(client, registration).status_code == 400
    _, valid = _registration(second)
    assert _verify(client, valid).status_code == 200


def test_unbound_legacy_registration_challenge_cannot_enroll(client, temp_db):
    _, _, identity = temp_db
    challenge = b"unbound-registration-challenge"
    identity.store_challenge(challenge, purpose="registration", user_id="user-matthew")
    _, registration = _registration(challenge)
    assert _verify(client, registration).status_code == 400
    assert not identity.get_user_credentials("user-matthew")


def test_challenge_expiry_never_extends_code_expiry(client, temp_db, monkeypatch):
    from datetime import timedelta

    from ledger.schema import connect
    from shared.timeutil import from_iso

    db, _, identity = temp_db
    code = identity.create_enrollment_code("user-matthew")
    conn = connect(db)
    expires = from_iso(conn.execute(
        "SELECT expires_at FROM enrollment_codes WHERE code_hash = ?", (hashlib_sha256(code),)
    ).fetchone()[0])
    monkeypatch.setattr("gateway.identity.now_utc", lambda: expires - timedelta(seconds=5))
    _begin(client, code)
    challenge_expiry = from_iso(conn.execute(
        "SELECT expires_at FROM auth_challenges WHERE purpose = 'registration'"
    ).fetchone()[0])
    assert challenge_expiry == expires
    monkeypatch.setattr("gateway.identity.now_utc", lambda: expires)
    response = client.post(
        "/api/auth/enroll/begin", json={"code": code}, headers={"Origin": TEST_ORIGIN}
    )
    assert response.status_code == 400
    conn.close()


def test_success_consumes_only_bound_code(client, temp_db):
    _, _, identity = temp_db
    first = identity.create_enrollment_code("user-matthew")
    second = identity.create_enrollment_code("user-matthew")
    _, registration_a = _registration(_begin(client, first))
    _, registration_b = _registration(_begin(client, second))
    assert _verify(client, registration_a).status_code == 200
    assert _verify(client, registration_b).status_code == 200


def test_additive_upgrade_preserves_identity_and_existing_state(temp_db):
    from ledger.schema import connect
    from ledger.seed import upgrade_db

    db, _, identity = temp_db
    identity.create_enrollment_code("user-matthew")
    identity.create_session("user-matthew")
    identity.store_credential(
        credential_id="preserved-credential", user_id="user-matthew", public_key=b"preserved",
        sign_count=7, transports=[], backup_eligible=False, backup_state=False,
    )
    conn = connect(db)
    tables = ["users", "accounts", "webauthn_credentials", "enrollment_codes", "user_sessions"]
    before = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in tables}
    conn.execute("DROP TABLE enrollment_challenges")
    conn.execute("DROP TABLE draft_calculations")
    conn.commit()
    upgrade_db(conn)
    upgrade_db(conn)
    for table in tables:
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] == before[table]
    assert conn.execute("SELECT COUNT(*) FROM enrollment_challenges").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM draft_calculations").fetchone()[0] == 0
    conn.close()
