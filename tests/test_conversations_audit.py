"""Tests for T-03: conversation snapshots and prototype audit chain.

Covers:
- Audit chain tampering detection (payload, step, prev_hash).
- Deleting a middle audit row is detected.
- Concurrent appends produce one valid gapless chain.
- Atomicity: audit failure rolls back ledger row and vice versa.
- Agent cannot impersonate user input or gateway audit decisions.
- Cross-user isolation (404 for other users' conversations/audit/audio).
- Correction supersedes a pending record bound to the old revision.
- Context bounds return CONTEXT_LIMIT_RESTATE.
- Oversized/non-WAV audio rejected; audio digest matches stored bytes.
- require_user_session without override returns 401 (fail closed).
- Transcript edit keeps original, bumps revision, changes hash.
- No route accepts user_* roles from a delegation token.
- Agent-event route rejects gateway.*/approval.*/execution.* steps.

Limitation (documented in test docstring): tail truncation and a
privileged full rewrite are NOT detectable without external checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.audit import (
    ALLOWED_AGENT_STEPS,
    GENESIS_HASH,
    REVISION_BOUND_TABLES,
    append_audit,
    verify_chain,
)
from gateway.auth.delegation import DelegationIssuer, _set_delegation_issuer
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore, compute_conversation_hash
from ledger.schema import connect

TEST_SECRET = "test-secret-key-0123456789abcdefghijklmnop"


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def temp_db():
    """Create a temp DB, init+seed it, wire both stores and the app."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    Path(db_path).unlink(missing_ok=True)

    bank_store = BankStore(db_path)
    bank_store.init_and_seed()

    # Also create a temp audio dir.
    audio_dir = tempfile.mkdtemp(prefix="dcta_audio_")

    conv_store = ConversationStore(db_path, audio_dir=audio_dir + "/")
    _set_bank_store(bank_store)
    _set_conversation_store(conv_store)

    issuer = DelegationIssuer(secret=TEST_SECRET)
    _set_delegation_issuer(issuer)

    # Override the fail-closed user session dependency.
    app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-matthew")

    yield db_path, bank_store, conv_store, issuer

    Path(db_path).unlink(missing_ok=True)
    # Clean up audio dir.
    import shutil

    shutil.rmtree(audio_dir, ignore_errors=True)
    _set_bank_store(None)
    _set_conversation_store(None)
    _set_delegation_issuer(None)
    app.dependency_overrides.clear()


@pytest.fixture()
def client(temp_db):
    return TestClient(app)


@pytest.fixture()
def conv_id(client):
    """Create a conversation and return its ID."""
    resp = client.post("/api/conversations", json={})
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


def ana_principal_override():
    """Switch the user session to ana for cross-user tests."""
    app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-ana")


# ── Fail-closed session ────────────────────────────────────────────


class TestFailClosedSession:
    def test_without_override_returns_401(self):
        """require_user_session without override must fail with 401."""
        # Temporarily clear overrides.
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            c = TestClient(app)
            resp = c.post("/api/conversations", json={})
            assert resp.status_code == 401
            # The real session dependency returns "missing session cookie"
            # (or "invalid or expired session") — no longer a T-05 placeholder.
            assert "session" in resp.json()["detail"].lower()
        finally:
            app.dependency_overrides.update(saved)


# ── Conversation lifecycle ─────────────────────────────────────────


class TestConversationLifecycle:
    def test_create_conversation(self, client):
        resp = client.post("/api/conversations", json={})
        assert resp.status_code == 200
        conv = resp.json()
        assert conv["conversation_id"].startswith("conv-")
        assert conv["revision"] == 0
        assert conv["turns"] == []
        assert len(conv["conversation_hash"]) == 64

    def test_append_text_turn(self, client, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "Transfer 50 to Mum"},
        )
        assert resp.status_code == 200
        turn = resp.json()
        assert turn["role"] == "user_text"
        assert turn["text"] == "Transfer 50 to Mum"
        assert turn["seq"] == 1

        # Check revision bumped.
        resp = client.get(f"/api/conversations/{conv_id}")
        assert resp.status_code == 200
        conv = resp.json()
        assert conv["revision"] == 1
        assert len(conv["turns"]) == 1

    def test_append_multiple_turns_revision_increments(self, client, conv_id):
        for i in range(3):
            client.post(
                f"/api/conversations/{conv_id}/text",
                json={"text": f"turn {i}"},
            )
        resp = client.get(f"/api/conversations/{conv_id}")
        conv = resp.json()
        assert conv["revision"] == 3
        assert len(conv["turns"]) == 3

    def test_conversation_not_found_404(self, client):
        resp = client.post(
            "/api/conversations/conv-nonexistent/text",
            json={"text": "hello"},
        )
        assert resp.status_code == 404

    def test_oversized_text_rejected(self, client, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "x" * 2001},
        )
        assert resp.status_code == 422  # Pydantic max_length


# ── Transcript edit ────────────────────────────────────────────────


class TestTranscriptEdit:
    def test_edit_keeps_original_and_bumps_revision(self, client, conv_id):
        # Original text turn.
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "Transfer fifty to Mum"},
        )
        assert resp.status_code == 200
        original_turn_id = resp.json()["turn_id"]

        # Edit it.
        resp = client.post(
            f"/api/conversations/{conv_id}/transcript-edit",
            json={"edits_turn_id": original_turn_id, "text": "Transfer fifteen to Mum"},
        )
        assert resp.status_code == 200
        edit_turn = resp.json()
        assert edit_turn["role"] == "user_transcript_edit"
        assert edit_turn["edits_turn_id"] == original_turn_id
        assert edit_turn["text"] == "Transfer fifteen to Mum"
        assert edit_turn["seq"] == 2

        # Get conversation — both turns present.
        resp = client.get(f"/api/conversations/{conv_id}")
        conv = resp.json()
        assert conv["revision"] == 2
        assert len(conv["turns"]) == 2
        original = conv["turns"][0]
        edited = conv["turns"][1]
        assert original["text"] == "Transfer fifty to Mum"
        assert edited["text"] == "Transfer fifteen to Mum"
        assert edited["edits_turn_id"] == original_turn_id

        # Hash changed.
        assert conv["conversation_hash"] != compute_conversation_hash(conv_id, 0, [])

    def test_edit_nonexistent_turn_404(self, client, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/transcript-edit",
            json={"edits_turn_id": "turn-nonexistent", "text": "hello"},
        )
        assert resp.status_code == 404


# ── Context bounds ─────────────────────────────────────────────────


class TestContextBounds:
    def test_max_turns_returns_context_limit(self, client, conv_id):
        """Exceeding max turns returns CONTEXT_LIMIT_RESTATE."""
        # Fill to max (40 turns).
        for i in range(40):
            resp = client.post(
                f"/api/conversations/{conv_id}/text",
                json={"text": f"turn {i}"},
            )
            assert resp.status_code == 200

        # 41st turn should fail.
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "overflow"},
        )
        assert resp.status_code == 413
        detail = resp.json()["detail"]
        # detail is a dict when using JSON body for error
        if isinstance(detail, dict):
            assert detail["code"] == "CONTEXT_LIMIT_RESTATE"
        else:
            assert "CONTEXT_LIMIT_RESTATE" in str(detail)

    def test_max_total_chars_returns_context_limit(self, client, conv_id):
        """Exceeding total character budget returns CONTEXT_LIMIT_RESTATE."""
        # Each turn is 500 chars; 20,000 / 500 = 40 turns at max chars.
        # Use 1000-char turns: 20 turns = 20,000 chars exactly.
        for _i in range(20):
            resp = client.post(
                f"/api/conversations/{conv_id}/text",
                json={"text": "x" * 1000},
            )
            assert resp.status_code == 200

        # 21st turn (1001 chars) exceeds 20,000 total.
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "x" * 1000},
        )
        assert resp.status_code == 413


# ── Audio evidence ─────────────────────────────────────────────────


class TestAudioEvidence:
    def _make_wav(self, duration_ms: int = 100) -> bytes:
        """Create a minimal valid WAV file."""
        sample_rate = 16000
        channels = 1
        bits_per_sample = 16
        num_samples = int(sample_rate * duration_ms / 1000)
        data_size = num_samples * channels * (bits_per_sample // 8)
        # Silence samples.
        data = b"\x00\x00" * num_samples
        # RIFF header.
        header = b"RIFF"
        header += (36 + data_size).to_bytes(4, "little")
        header += b"WAVE"
        # fmt chunk.
        header += b"fmt "
        header += (16).to_bytes(4, "little")  # chunk size
        header += (1).to_bytes(2, "little")  # audio format = PCM
        header += channels.to_bytes(2, "little")
        header += sample_rate.to_bytes(4, "little")
        header += (sample_rate * channels * bits_per_sample // 8).to_bytes(4, "little")
        header += (channels * bits_per_sample // 8).to_bytes(2, "little")
        header += bits_per_sample.to_bytes(2, "little")
        # data chunk.
        header += b"data"
        header += data_size.to_bytes(4, "little")
        return header + data

    def test_upload_wav(self, client, conv_id):
        wav_bytes = self._make_wav()
        resp = client.post(
            f"/api/conversations/{conv_id}/audio",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
            params={"consent": "true"},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["content_type"] == "audio/wav"
        assert result["size_bytes"] == len(wav_bytes)
        # Digest matches.
        assert result["sha256_digest"] == hashlib.sha256(wav_bytes).hexdigest()
        # Duration parsed.
        assert result["duration_ms"] is not None
        assert result["duration_ms"] > 0

    def test_non_wav_rejected(self, client, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/audio",
            files={"file": ("test.mp3", b"fake mp3", "audio/mpeg")},
        )
        assert resp.status_code == 400

    def test_oversized_audio_rejected(self, client, conv_id):
        # 4 MB > 3 MB limit.
        big_bytes = b"\x00" * (4 * 1024 * 1024)
        resp = client.post(
            f"/api/conversations/{conv_id}/audio",
            files={"file": ("big.wav", big_bytes, "audio/wav")},
        )
        assert resp.status_code == 413


# ── Audit chain ────────────────────────────────────────────────────


class TestAuditChain:
    def _connect(self, db_path: str):
        return connect(db_path)

    def test_genesis_prev_hash_is_zeros(self, temp_db):
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            # Append one event.
            conn.execute("BEGIN IMMEDIATE")
            append_audit(
                conn,
                source="gateway",
                step="gateway.test",
                payload={"msg": "first"},
            )
            conn.commit()

            # Check genesis.
            row = conn.execute(
                "SELECT prev_hash, row_hash FROM audit_events WHERE seq = 1"
            ).fetchone()
            assert row["prev_hash"] == GENESIS_HASH
            assert len(row["row_hash"]) == 64
        finally:
            conn.close()

    def test_chain_verifies_clean(self, temp_db):
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            for i in range(5):
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.test",
                    payload={"i": i},
                )
                conn.commit()

            result = verify_chain(conn)
            assert result["ok"] is True
            assert result["first_bad_seq"] is None
        finally:
            conn.close()

    def test_tampering_payload_detected(self, temp_db):
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            for i in range(3):
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.test",
                    payload={"i": i},
                )
                conn.commit()

            # Tamper with row 2's payload.
            conn.execute(
                "UPDATE audit_events SET payload_json = ? WHERE seq = 2",
                (json.dumps({"tampered": True}),),
            )
            conn.commit()

            result = verify_chain(conn)
            assert result["ok"] is False
            assert result["first_bad_seq"] == 2
        finally:
            conn.close()

    def test_tampering_step_detected(self, temp_db):
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            for i in range(3):
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.test",
                    payload={"i": i},
                )
                conn.commit()

            conn.execute("UPDATE audit_events SET step = 'approval.forged' WHERE seq = 2")
            conn.commit()

            result = verify_chain(conn)
            assert result["ok"] is False
            assert result["first_bad_seq"] == 2
        finally:
            conn.close()

    def test_tampering_prev_hash_detected(self, temp_db):
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            for i in range(4):
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.test",
                    payload={"i": i},
                )
                conn.commit()

            conn.execute(
                "UPDATE audit_events SET prev_hash = ? WHERE seq = 3",
                ("a" * 64,),
            )
            conn.commit()

            result = verify_chain(conn)
            assert result["ok"] is False
            assert result["first_bad_seq"] == 3
        finally:
            conn.close()

    def test_deleting_middle_row_detected(self, temp_db):
        """Deleting a middle row creates a seq gap that verify_chain catches.

        Note: tail truncation (deleting the last row or rows from the end)
        and a privileged complete rewrite (all rows + hashes rewritten
        consistently) are NOT detectable without external checkpoints.
        These are documented limitations of a local hash chain.
        """
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            for i in range(5):
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.test",
                    payload={"i": i},
                )
                conn.commit()

            # Delete row 3.
            conn.execute("DELETE FROM audit_events WHERE seq = 3")
            conn.commit()

            result = verify_chain(conn)
            assert result["ok"] is False
            # The gap is detected at seq 4 (expected 3, got 4).
            assert result["first_bad_seq"] is not None
        finally:
            conn.close()

    def test_concurrent_appends_produce_valid_chain(self, temp_db):
        """Multiple threads appending concurrently produce one gapless chain."""
        db_path, _, _, _ = temp_db
        num_threads = 10
        events_per_thread = 5

        def append_worker():
            conn = self._connect(db_path)
            try:
                for _ in range(events_per_thread):
                    conn.execute("BEGIN IMMEDIATE")
                    append_audit(
                        conn,
                        source="gateway",
                        step="gateway.concurrent",
                        payload={},
                    )
                    conn.commit()
            finally:
                conn.close()

        threads = [threading.Thread(target=append_worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        conn = self._connect(db_path)
        try:
            total = num_threads * events_per_thread
            count = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
            assert count == total

            result = verify_chain(conn)
            assert result["ok"] is True
        finally:
            conn.close()

    def test_atomicity_audit_failure_rolls_back_ledger(self, temp_db):
        """Inside one BEGIN IMMEDIATE, write a test ledger row + append_audit.

        Force the audit append to raise → the ledger row is rolled back.
        """
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            # Create a test table to write to.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS test_ledger (  id INTEGER PRIMARY KEY, data TEXT)"
            )
            conn.commit()

            # Path 1: audit failure rolls back ledger.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO test_ledger (data) VALUES (?)", ("should-rollback",))
            # Force audit to fail by closing the table — monkeypatch.
            # Simulate: drop the audit_events table after the insert.
            conn.execute("DROP TABLE audit_events")
            with pytest.raises(sqlite3.OperationalError):
                append_audit(
                    conn,
                    source="gateway",
                    step="gateway.atomicity",
                    payload={},
                )
            conn.rollback()

            # Verify test_ledger is empty.
            row = conn.execute("SELECT COUNT(*) AS c FROM test_ledger").fetchone()
            assert row["c"] == 0

            # Recreate audit_events for subsequent tests.
            from ledger.schema import init_db

            init_db(conn)
        finally:
            conn.close()

    def test_atomicity_both_commit(self, temp_db):
        """The reverse path: both ledger row and audit commit together."""
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS test_ledger2 (  id INTEGER PRIMARY KEY, data TEXT)"
            )
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO test_ledger2 (data) VALUES (?)", ("committed",))
            seq = append_audit(
                conn,
                source="gateway",
                step="gateway.atomicity_ok",
                payload={"msg": "both commit"},
            )
            conn.commit()

            assert seq >= 1
            row = conn.execute("SELECT data FROM test_ledger2").fetchone()
            assert row["data"] == "committed"
            audit = conn.execute("SELECT step FROM audit_events WHERE seq = ?", (seq,)).fetchone()
            assert audit["step"] == "gateway.atomicity_ok"
        finally:
            conn.close()

    def test_verify_chain_scoped_to_conversation(self, temp_db):
        """verify_chain with conversation_id only checks that conversation's rows."""
        db_path, _, _, _ = temp_db
        conn = self._connect(db_path)
        try:
            # Append events for two conversations.
            for conv in ["conv-A", "conv-A", "conv-B", "conv-A"]:
                conn.execute("BEGIN IMMEDIATE")
                append_audit(
                    conn,
                    source="gateway",
                    conversation_id=conv,
                    step="gateway.test",
                    payload={},
                )
                conn.commit()

            # Verify conv-A (3 events).
            result_a = verify_chain(conn, conversation_id="conv-A")
            assert result_a["ok"] is True

            # Verify conv-B (1 event).
            result_b = verify_chain(conn, conversation_id="conv-B")
            assert result_b["ok"] is True
        finally:
            conn.close()


# ── Agent impersonation prevention ─────────────────────────────────


class TestAgentImpersonation:
    def test_no_route_accepts_user_role_from_delegation(self, temp_db):
        """No route accepts a user_* role from a delegation token.

        The internal /questions route only creates app_question.
        There is no internal route to append user turns.  This is verified
        by checking that the agent can only propose questions, not user turns.
        """
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        # Create a conversation as a user.
        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        # Issue an agent token with conversation:propose_question.
        agent_token = issuer.issue_token(
            "user-matthew",
            ["conversation:propose_question"],
            conversation_id=conv_id,
        )

        # Agent can propose a question.
        resp = client.post(
            f"/internal/conversations/{conv_id}/questions",
            json={"text": "Which account?"},
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "app_question"

        # There is NO route to append user_text from a delegation token.
        # Remove the test session override so only the agent's token is presented:
        # user routes must fail closed (401) rather than accept a delegation token.
        app.dependency_overrides.pop(require_user_session, None)
        for path in ("text", "voice", "transcript-edit", "answer"):
            resp = client.post(
                f"/api/conversations/{conv_id}/{path}",
                json={"text": "fabricated user turn"},
                headers={"Authorization": f"Bearer {agent_token}"},
            )
            assert resp.status_code == 401, f"/{path} accepted an agent token"
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(
            user_id="user-matthew"
        )
        snapshot = conv_store.get_snapshot(conv_id)
        assert all(not t["role"].startswith("user_") for t in snapshot["turns"])

    def test_agent_event_rejects_gateway_steps(self, temp_db):
        """The agent-event route rejects gateway.* step names."""
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        token = issuer.issue_token("user-matthew", ["audit:append_agent"])

        for step in ["gateway.test", "approval.issue", "execution.commit"]:
            resp = client.post(
                "/internal/audit/agent-events",
                json={"step": step, "payload": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403, f"step {step} should be rejected"

    def test_agent_event_rejects_non_allowlisted_steps(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        token = issuer.issue_token("user-matthew", ["audit:append_agent"])

        resp = client.post(
            "/internal/audit/agent-events",
            json={"step": "agent.fabricated_step", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_agent_event_accepts_allowlisted_steps(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)
        conv_id = client.post("/api/conversations", json={}).json()["conversation_id"]
        token = issuer.issue_token("user-matthew", ["audit:append_agent"], conversation_id=conv_id)

        for step in ALLOWED_AGENT_STEPS:
            resp = client.post(
                "/internal/audit/agent-events",
                json={"step": step, "payload": {"test": True}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, f"step {step} should be accepted"
            assert resp.json()["seq"] >= 1

    def test_agent_event_requires_conversation_bound_token(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)
        token = issuer.issue_token("user-matthew", ["audit:append_agent"])
        resp = client.post(
            "/internal/audit/agent-events",
            json={"step": "agent.extract", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_agent_event_cannot_target_other_conversation(self, temp_db):
        """An agent bound to conversation A cannot write into conversation B's audit."""
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)
        conv_a = client.post("/api/conversations", json={}).json()["conversation_id"]
        conv_b = client.post("/api/conversations", json={}).json()["conversation_id"]
        token = issuer.issue_token("user-matthew", ["audit:append_agent"], conversation_id=conv_a)
        resp = client.post(
            "/internal/audit/agent-events",
            json={"step": "agent.extract", "payload": {}, "conversation_id": conv_b},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_agent_event_other_users_conversation_404(self, temp_db):
        """A token naming another user's conversation is rejected (no cross-user writes)."""
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)
        conv_id = client.post("/api/conversations", json={}).json()["conversation_id"]
        token = issuer.issue_token("user-ana", ["audit:append_agent"], conversation_id=conv_id)
        resp = client.post(
            "/internal/audit/agent-events",
            json={"step": "agent.extract", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_append_audit_outside_transaction_raises(self, temp_db):
        from ledger.schema import connect

        db_path, bank_store, conv_store, issuer = temp_db
        conn = connect(db_path)
        try:
            with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
                append_audit(conn, source="gateway", step="gateway.test", payload={})
        finally:
            conn.close()

    def test_snapshot_wrong_conversation_403(self, temp_db):
        """Snapshot with a token for another conversation → 403."""
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        # Create two conversations.
        resp = client.post("/api/conversations", json={})
        conv_a = resp.json()["conversation_id"]
        resp = client.post("/api/conversations", json={})
        conv_b = resp.json()["conversation_id"]

        # Token bound to conv_a.
        token = issuer.issue_token(
            "user-matthew",
            ["conversation:read"],
            conversation_id=conv_a,
        )

        # Try to read conv_b with conv_a's token.
        resp = client.get(
            f"/internal/conversations/{conv_b}/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_snapshot_wrong_user_404(self, temp_db):
        """Snapshot with a token for the right conversation but wrong user → 404."""
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        # Matthew creates a conversation.
        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        # Ana gets a token for that conversation (but doesn't own it).
        token = issuer.issue_token(
            "user-ana",
            ["conversation:read"],
            conversation_id=conv_id,
        )

        resp = client.get(
            f"/internal/conversations/{conv_id}/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_snapshot_wrong_scope_403(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        token = issuer.issue_token(
            "user-matthew",
            ["bank:read"],  # wrong scope
            conversation_id=conv_id,
        )

        resp = client.get(
            f"/internal/conversations/{conv_id}/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


# ── Cross-user isolation ───────────────────────────────────────────


class TestCrossUserIsolation:
    def test_user_b_cannot_read_user_a_conversation(self, temp_db):
        """User B cannot read user A's conversation (404)."""
        db_path, _, _, _ = temp_db
        client = TestClient(app)

        # Matthew creates a conversation.
        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]
        client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "matthew's private data"},
        )

        # Switch to ana.
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-ana")

        # Ana tries to read Matthew's conversation.
        resp = client.get(f"/api/conversations/{conv_id}")
        assert resp.status_code == 404

        # Ana tries to read audit.
        resp = client.get(f"/api/conversations/{conv_id}/audit")
        assert resp.status_code == 404

        # Ana tries to verify chain.
        resp = client.get(f"/api/conversations/{conv_id}/audit/verify")
        assert resp.status_code == 404

        # Restore.
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(
            user_id="user-matthew"
        )

    def test_user_b_cannot_append_to_user_a_conversation(self, temp_db):
        db_path, _, _, _ = temp_db
        client = TestClient(app)

        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        # Switch to ana.
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-ana")

        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "ana trying to inject"},
        )
        assert resp.status_code == 404

        # Restore.
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(
            user_id="user-matthew"
        )

    def test_user_b_cannot_upload_audio_to_user_a_conversation(self, temp_db):
        db_path, _, _, _ = temp_db
        client = TestClient(app)

        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-ana")

        wav = b"RIFF" + b"\x00" * 40  # minimal-ish
        resp = client.post(
            f"/api/conversations/{conv_id}/audio",
            files={"file": ("test.wav", wav, "audio/wav")},
        )
        assert resp.status_code == 404

        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(
            user_id="user-matthew"
        )


# ── Supersession ───────────────────────────────────────────────────


class TestSupersession:
    def test_correction_supersedes_pending_record(self, temp_db):
        """A transcript edit bumps revision and supersedes a pending record
        bound to the old revision.

        Uses a test-only table registered in REVISION_BOUND_TABLES.
        """
        db_path, _, _, _ = temp_db
        conn = connect(db_path)
        try:
            # Create a test-only revision-bound table.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_drafts (
                    draft_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                )
                """
            )
            conn.commit()

            # Register it in REVISION_BOUND_TABLES.
            REVISION_BOUND_TABLES["test_drafts"] = {
                "revision_column": "revision",
                "status_column": "status",
                "conversation_id_column": "conversation_id",
                "superseded_value": "SUPERSEDED",
            }

            # Create a conversation with the store.
            conv_store = temp_db[2]
            conv = conv_store.create_conversation("user-matthew")
            conv_id = conv["conversation_id"]
            rev0 = conv["revision"]  # 0

            # Insert a pending draft bound to revision 0.
            conn.execute(
                "INSERT INTO test_drafts (draft_id, conversation_id, revision, status) "
                "VALUES (?, ?, ?, 'PENDING')",
                ("draft-1", conv_id, rev0),
            )
            conn.commit()

            # Append a text turn → revision 1.
            conv_store.append_text_turn("user-matthew", conv_id, "first turn")

            # Check the draft is now SUPERSEDED.
            row = conn.execute(
                "SELECT status FROM test_drafts WHERE draft_id = 'draft-1'"
            ).fetchone()
            assert row["status"] == "SUPERSEDED"

        finally:
            # Clean up registry.
            REVISION_BOUND_TABLES.pop("test_drafts", None)
            conn.close()

    def test_supersession_audited(self, temp_db):
        """Supersession is audited."""
        db_path, _, _, _ = temp_db
        conn = connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_drafts2 (
                    draft_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                )
                """
            )
            conn.commit()

            REVISION_BOUND_TABLES["test_drafts2"] = {
                "revision_column": "revision",
                "status_column": "status",
                "conversation_id_column": "conversation_id",
                "superseded_value": "SUPERSEDED",
            }

            conv_store = temp_db[2]
            conv = conv_store.create_conversation("user-matthew")
            conv_id = conv["conversation_id"]

            conn.execute(
                "INSERT INTO test_drafts2 (draft_id, conversation_id, revision, status) "
                "VALUES (?, ?, 0, 'PENDING')",
                ("draft-2", conv_id),
            )
            conn.commit()

            conv_store.append_text_turn("user-matthew", conv_id, "turn")

            # Check audit for supersede.
            row = conn.execute(
                "SELECT step FROM audit_events WHERE step = 'gateway.supersede' "
                "AND conversation_id = ?",
                (conv_id,),
            ).fetchone()
            assert row is not None
        finally:
            REVISION_BOUND_TABLES.pop("test_drafts2", None)
            conn.close()

    def test_supersession_no_pending_records(self, temp_db):
        """When there are no pending records, supersession is a no-op."""
        db_path, _, _, _ = temp_db
        conn = connect(db_path)
        try:
            REVISION_BOUND_TABLES["test_drafts3"] = {
                "revision_column": "revision",
                "status_column": "status",
                "conversation_id_column": "conversation_id",
                "superseded_value": "SUPERSEDED",
            }
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_drafts3 (
                    draft_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                )
                """
            )
            conn.commit()

            conv_store = temp_db[2]
            conv = conv_store.create_conversation("user-matthew")
            conv_id = conv["conversation_id"]

            # No pending drafts inserted. Just append a turn.
            conv_store.append_text_turn("user-matthew", conv_id, "turn")

            # No supersede audit event.
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events "
                "WHERE step = 'gateway.supersede' AND conversation_id = ?",
                (conv_id,),
            ).fetchone()
            assert row["c"] == 0
        finally:
            REVISION_BOUND_TABLES.pop("test_drafts3", None)
            conn.close()


# ── Audit API endpoints ────────────────────────────────────────────


class TestAuditAPI:
    def test_get_audit_events(self, client, conv_id):
        # Create some audit events by appending turns.
        client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "hello"},
        )

        resp = client.get(f"/api/conversations/{conv_id}/audit")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 2  # create + revision

    def test_verify_audit_chain_endpoint(self, client, conv_id):
        client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "hello"},
        )

        resp = client.get(f"/api/conversations/{conv_id}/audit/verify")
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is True
        assert result["first_bad_seq"] is None


# ── Snapshot and questions (internal) ───────────────────────────────


class TestSnapshotAndQuestions:
    def test_snapshot_returns_ordered_turns(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        # Create conversation and add turns.
        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]
        client.post(f"/api/conversations/{conv_id}/text", json={"text": "turn 1"})
        client.post(f"/api/conversations/{conv_id}/text", json={"text": "turn 2"})

        # Agent reads snapshot.
        token = issuer.issue_token(
            "user-matthew",
            ["conversation:read"],
            conversation_id=conv_id,
        )
        resp = client.get(
            f"/internal/conversations/{conv_id}/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["conversation_id"] == conv_id
        assert snap["revision"] == 2
        assert len(snap["turns"]) == 2
        assert snap["turns"][0]["seq"] == 1
        assert snap["turns"][1]["seq"] == 2

    def test_question_does_not_bump_revision(self, temp_db):
        db_path, bank_store, conv_store, issuer = temp_db
        client = TestClient(app)

        resp = client.post("/api/conversations", json={})
        conv_id = resp.json()["conversation_id"]

        # Get initial revision.
        resp = client.get(f"/api/conversations/{conv_id}")
        initial_rev = resp.json()["revision"]

        # Agent proposes a question.
        token = issuer.issue_token(
            "user-matthew",
            ["conversation:propose_question"],
            conversation_id=conv_id,
        )
        resp = client.post(
            f"/internal/conversations/{conv_id}/questions",
            json={"text": "Which account?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "app_question"

        # Revision should NOT have changed.
        resp = client.get(f"/api/conversations/{conv_id}")
        assert resp.json()["revision"] == initial_rev

    def test_answer_tied_to_question(self, client, conv_id, temp_db):
        """A user_answer turn is tied to the app_question it answers."""
        _, _, _, issuer = temp_db

        # Agent proposes a question.
        token = issuer.issue_token(
            "user-matthew",
            ["conversation:propose_question"],
            conversation_id=conv_id,
        )
        resp = client.post(
            f"/internal/conversations/{conv_id}/questions",
            json={"text": "Which account?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        question_id = resp.json()["turn_id"]

        # User answers.
        resp = client.post(
            f"/api/conversations/{conv_id}/answer",
            json={"answers_question_id": question_id, "text": "Savings"},
        )
        assert resp.status_code == 200
        answer = resp.json()
        assert answer["role"] == "user_answer"
        assert answer["answers_question_id"] == question_id

    def test_answer_nonexistent_question_404(self, client, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/answer",
            json={"answers_question_id": "turn-nonexistent", "text": "answer"},
        )
        assert resp.status_code == 404

    def test_answer_non_question_404(self, client, conv_id):
        """Answering a non-question turn should fail."""
        # First create a user_text turn.
        resp = client.post(
            f"/api/conversations/{conv_id}/text",
            json={"text": "a text turn"},
        )
        text_turn_id = resp.json()["turn_id"]

        # Try to answer it as if it were a question.
        resp = client.post(
            f"/api/conversations/{conv_id}/answer",
            json={"answers_question_id": text_turn_id, "text": "answer"},
        )
        assert resp.status_code == 404
