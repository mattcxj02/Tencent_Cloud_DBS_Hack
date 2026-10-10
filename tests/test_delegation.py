"""Tests for delegation token issuance and verification."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from gateway.auth.delegation import DelegationIssuer, Principal

TEST_SECRET = "test-secret-key-0123456789abcdefghijklmnop"
OTHER_SECRET = "other-secret-key-0123456789abcdefghijklmno"


class TestDelegationToken:
    def test_valid_token(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        token = issuer.issue_token("user-matthew", ["bank:read"])
        principal = issuer.verify_token(token)
        assert principal.user_id == "user-matthew"
        assert principal.has_scope("bank:read")
        assert not principal.has_scope("bank:write")

    def test_expired_token(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        # Issue with 1 second TTL, then wait for expiry
        token = issuer.issue_token("user-matthew", ["bank:read"], ttl_seconds=1)
        time.sleep(2)
        with pytest.raises(HTTPException) as exc_info:
            issuer.verify_token(token)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail

    def test_tampered_payload(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        token = issuer.issue_token("user-matthew", ["bank:read"])
        # Tamper with the payload portion
        prefix_len = len("dcta_")
        body_sig = token[prefix_len:]
        body, sig = body_sig.split(".")
        # Flip a character in the body
        import base64

        raw = base64.urlsafe_b64decode(body)
        # Change user_id character
        tampered = bytearray(raw)
        tampered[5] ^= 0x01
        tampered_body = base64.urlsafe_b64encode(bytes(tampered)).decode("ascii")
        tampered_token = f"dcta_{tampered_body}.{sig}"
        with pytest.raises(HTTPException) as exc_info:
            issuer.verify_token(tampered_token)
        assert exc_info.value.status_code == 401
        assert "signature" in exc_info.value.detail

    def test_wrong_scope(self) -> None:
        """A token with bank:write but not bank:read should fail scope check."""
        issuer = DelegationIssuer(secret=TEST_SECRET)
        token = issuer.issue_token("user-matthew", ["bank:write"])
        # Token itself verifies fine
        principal = issuer.verify_token(token)
        assert principal.has_scope("bank:write")
        assert not principal.has_scope("bank:read")
        # require_principal would 403 — simulate what the dependency does
        with pytest.raises(HTTPException) as exc_info:
            if not principal.has_scope("bank:read"):
                raise HTTPException(status_code=403, detail="missing required scope: bank:read")
        assert exc_info.value.status_code == 403

    def test_wrong_secret(self) -> None:
        issuer1 = DelegationIssuer(secret=TEST_SECRET)
        issuer2 = DelegationIssuer(secret=OTHER_SECRET)
        token = issuer1.issue_token("user-matthew", ["bank:read"])
        with pytest.raises(HTTPException) as exc_info:
            issuer2.verify_token(token)
        assert exc_info.value.status_code == 401
        assert "signature" in exc_info.value.detail

    def test_malformed_token(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            issuer.verify_token("not-a-valid-token")
        assert exc_info.value.status_code == 401
        assert "format" in exc_info.value.detail

    def test_no_prefix(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            issuer.verify_token("xxx.yyy")
        assert exc_info.value.status_code == 401

    def test_token_carries_conversation_id(self) -> None:
        issuer = DelegationIssuer(secret=TEST_SECRET)
        token = issuer.issue_token("user-matthew", ["bank:read"], conversation_id="conv-123")
        principal = issuer.verify_token(token)
        assert principal.conversation_id == "conv-123"

    def test_ephemeral_secret_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger="dcta.gateway.delegation")
        issuer = DelegationIssuer(secret=None)
        assert issuer.ephemeral is True
        assert "GATEWAY_DELEGATION_SECRET" in caplog.text

    def test_user_cannot_be_overridden_by_header(self) -> None:
        """The user comes only from the verified token, not from any header."""
        # This is tested at the API level in test_bank_read_api.py
        # Here we just verify the Principal dataclass is immutable
        principal = Principal(
            user_id="user-matthew",
            scopes=frozenset({"bank:read"}),
        )
        # Principal is frozen
        with pytest.raises(AttributeError):
            principal.user_id = "user-ana"  # type: ignore[misc]


@pytest.mark.parametrize("weak", ["short", "replace-with-a-strong-random-secret-value-xyz"])
def test_weak_or_placeholder_secret_rejected(weak: str) -> None:
    with pytest.raises(ValueError, match="GATEWAY_DELEGATION_SECRET"):
        DelegationIssuer(secret=weak)
