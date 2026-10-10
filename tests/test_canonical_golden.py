"""Golden fixtures for canonical bytes and challenge hex.

A refactor that changes canonical_bytes output must fail these tests.
The expected values are computed from fixed constant inputs and committed
in the test source.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from shared.canonical import (
    approval_challenge_hex,
    bundle_hash,
    canonical_bytes,
)
from shared.money import Money
from shared.schemas import (
    Action,
    DisplayMetadata,
    EquityTerms,
    FrozenBundle,
    Leg,
    Lineage,
    SigningEnvelope,
    SourceRef,
    TargetType,
)

# ---------------------------------------------------------------------------
# Fixed constants
# ---------------------------------------------------------------------------

FIXED_BUNDLE_ID = UUID("11111111-1111-1111-1111-111111111111")
FIXED_CONVERSATION_ID = "conv-fixed-001"
FIXED_CONVERSATION_HASH = "abcdef1234567890"
FIXED_OWNER = "user-matthew"
FIXED_CREATED_AT = "2026-10-10T12:00:00.000Z"

FIXED_USER_ID = "user-matthew"
FIXED_CREDENTIAL_ID = "cred-001"
FIXED_OPERATION_ID = "op-001"
FIXED_MANDATE_VERSION = 1
FIXED_NONCE = "00" * 32  # 64 hex chars = 32 bytes, all zeros
FIXED_ISSUED_AT = "2026-10-10T12:00:00.000Z"
FIXED_EXPIRES_AT = "2026-10-10T12:00:01.000Z"  # 1 second later

FIXED_LINEAGE = Lineage(
    source_refs=[
        SourceRef(
            kind="user_turn",
            turn_id="turn-1",
            span_start=0,
            span_end=5,
        )
    ]
)


def _make_leg(
    leg_id: str = "leg-1",
    action: Action = Action.transfer,
    source_account_id: str = "acct-savings",
    target_type: TargetType = TargetType.own_account,
    target_id: str = "acct-current",
    amount: str = "50.00",
    display_name: str = "Current Account",
    masked_suffix: str = "1234",
    equity_terms: EquityTerms | None = None,
) -> Leg:
    # For buy_equity, auto-create valid equity terms if not provided.
    if action == Action.buy_equity and equity_terms is None:
        equity_terms = EquityTerms(
            budget=Money("1000.00"),
            quote_id="Q-FIXED",
            unit_price=Money("3.50"),
            quoted_at="2026-10-10T10:00:00.000Z",
            quote_expires_at="2026-10-10T10:05:00.000Z",
            quantity=285,
            actual_debit=Money("997.50"),
            residual_cash=Money("2.50"),
        )
        # Override amount to match actual_debit for buy_equity.
        amount = equity_terms.actual_debit.to_str()
    return Leg(
        leg_id=leg_id,
        action=action,
        source_account_id=source_account_id,
        target_type=target_type,
        target_id=target_id,
        display_metadata=DisplayMetadata(
            display_name=display_name,
            masked_account_suffix=masked_suffix,
        ),
        amount=Money(amount),
        lineage=FIXED_LINEAGE,
        equity_terms=equity_terms,
    )


def _make_bundle(
    legs: list[Leg] | None = None,
    bundle_id: UUID = FIXED_BUNDLE_ID,
    revision: int = 1,
    owner_user_id: str = FIXED_OWNER,
    conversation_id: str = FIXED_CONVERSATION_ID,
    conversation_revision: int = 1,
    conversation_hash: str = FIXED_CONVERSATION_HASH,
    created_at: str = FIXED_CREATED_AT,
) -> FrozenBundle:
    return FrozenBundle(
        bundle_id=bundle_id,
        revision=revision,
        owner_user_id=owner_user_id,
        conversation_id=conversation_id,
        conversation_revision=conversation_revision,
        conversation_hash=conversation_hash,
        created_at=created_at,
        legs=legs if legs is not None else [_make_leg()],
    )


def _make_envelope(
    bundle: FrozenBundle | None = None,
    operation: str = "execute_bundle",
    user_id: str = FIXED_USER_ID,
    credential_id: str = FIXED_CREDENTIAL_ID,
    operation_id: str = FIXED_OPERATION_ID,
    active_mandate_version: int = FIXED_MANDATE_VERSION,
    nonce: str = FIXED_NONCE,
    issued_at: str = FIXED_ISSUED_AT,
    expires_at: str = FIXED_EXPIRES_AT,
    conversation_id: str | None = FIXED_CONVERSATION_ID,
    conversation_revision: int | None = 1,
    conversation_hash: str | None = FIXED_CONVERSATION_HASH,
    payload: object | None = None,
) -> SigningEnvelope:
    b = bundle or _make_bundle()
    return SigningEnvelope(
        schema_version=1,
        operation=operation,
        user_id=user_id,
        credential_id=credential_id,
        operation_id=operation_id,
        payload=payload if payload is not None else b,
        conversation_id=conversation_id,
        conversation_revision=conversation_revision,
        conversation_hash=conversation_hash,
        active_mandate_version=active_mandate_version,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Pinned golden byte fixtures
#
# These exact values are computed once from the fixed constants above using
# the current implementation.  A refactor that changes canonical_bytes output
# (key ordering, separators, Money serialization, timestamp format, UUID
# casing, enum handling, array order) MUST fail these assertions.
# ---------------------------------------------------------------------------

#: Exact canonical_bytes for the fixed bundle.
PINNED_BUNDLE_CANONICAL = (
    b'{"bundle_id":"11111111-1111-1111-1111-111111111111",'
    b'"conversation_hash":"abcdef1234567890",'
    b'"conversation_id":"conv-fixed-001","conversation_revision":1,'
    b'"created_at":"2026-10-10T12:00:00.000Z",'
    b'"legs":[{"action":"transfer","amount":"50.00","currency":"SGD",'
    b'"dependencies":[],"display_metadata":{"display_name":"Current Account",'
    b'"masked_account_suffix":"1234"},"equity_terms":null,"leg_id":"leg-1",'
    b'"lineage":{"parent_refs":[],"source_refs":[{"kind":"user_turn",'
    b'"record_id":null,"span_end":5,"span_start":0,"turn_id":"turn-1"}]},'
    b'"reference":null,"source_account_id":"acct-savings",'
    b'"target_id":"acct-current","target_type":"own_account"}],'
    b'"owner_user_id":"user-matthew","revision":1}'
)

#: Exact bundle_hash hex for the fixed bundle.
PINNED_BUNDLE_HASH = "7a23ae293208e2cc3429fb8e4cc79c4f8ffbcbef48c50cc8ef86bb5d50799f70"

#: Exact canonical_bytes for the fixed envelope.
PINNED_ENVELOPE_CANONICAL = (
    b'{"active_mandate_version":1,"conversation_hash":"abcdef1234567890",'
    b'"conversation_id":"conv-fixed-001","conversation_revision":1,'
    b'"credential_id":"cred-001","expires_at":"2026-10-10T12:00:01.000Z",'
    b'"issued_at":"2026-10-10T12:00:00.000Z",'
    b'"nonce":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"operation":"execute_bundle","operation_id":"op-001",'
    b'"payload":{"bundle_id":"11111111-1111-1111-1111-111111111111",'
    b'"conversation_hash":"abcdef1234567890",'
    b'"conversation_id":"conv-fixed-001","conversation_revision":1,'
    b'"created_at":"2026-10-10T12:00:00.000Z",'
    b'"legs":[{"action":"transfer","amount":"50.00","currency":"SGD",'
    b'"dependencies":[],"display_metadata":{"display_name":"Current Account",'
    b'"masked_account_suffix":"1234"},"equity_terms":null,"leg_id":"leg-1",'
    b'"lineage":{"parent_refs":[],"source_refs":[{"kind":"user_turn",'
    b'"record_id":null,"span_end":5,"span_start":0,"turn_id":"turn-1"}]},'
    b'"reference":null,"source_account_id":"acct-savings",'
    b'"target_id":"acct-current","target_type":"own_account"}],'
    b'"owner_user_id":"user-matthew","revision":1},'
    b'"schema_version":1,"user_id":"user-matthew"}'
)

#: Exact approval_challenge_hex for the fixed envelope.
PINNED_ENVELOPE_CHALLENGE = "8a6250870839b4ed35aec25206262f96ad0e0ce4c0e04524041f3472fb1c73db"


# ---------------------------------------------------------------------------
# Golden tests
# ---------------------------------------------------------------------------


class TestGoldenCanonical:
    """Stable golden fixtures — a refactor changing bytes must fail."""

    def test_canonical_bytes_bundle_exact(self) -> None:
        """Exact canonical_bytes for the fixed bundle."""
        bundle = _make_bundle()
        cb = canonical_bytes(bundle)
        assert cb == PINNED_BUNDLE_CANONICAL
        # Verify it's valid JSON with sorted keys.
        parsed = json.loads(cb)
        assert isinstance(parsed, dict)
        assert list(parsed.keys()) == sorted(parsed.keys())
        assert parsed["bundle_id"] == "11111111-1111-1111-1111-111111111111"
        assert parsed["legs"][0]["amount"] == "50.00"
        assert parsed["legs"][0]["currency"] == "SGD"

    def test_canonical_bytes_envelope_exact(self) -> None:
        """Exact canonical_bytes for the fixed envelope."""
        env = _make_envelope()
        cb = canonical_bytes(env)
        assert cb == PINNED_ENVELOPE_CANONICAL
        parsed = json.loads(cb)
        assert isinstance(parsed, dict)
        assert list(parsed.keys()) == sorted(parsed.keys())
        assert parsed["schema_version"] == 1
        assert parsed["operation"] == "execute_bundle"
        assert parsed["nonce"] == "00" * 32

    def test_bundle_hash_exact(self) -> None:
        """Exact bundle hash for the fixed bundle."""
        bundle = _make_bundle()
        h = bundle_hash(bundle)
        assert h == PINNED_BUNDLE_HASH
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_challenge_hex_exact(self) -> None:
        """Exact approval challenge hex for the fixed envelope."""
        env = _make_envelope()
        ch = approval_challenge_hex(env)
        assert ch == PINNED_ENVELOPE_CHALLENGE
        assert len(ch) == 64
        assert all(c in "0123456789abcdef" for c in ch)

    def test_bundle_hash_differs_from_envelope_challenge(self) -> None:
        """Bundle hash != approval challenge for the same bundle."""
        bundle = _make_bundle()
        env = _make_envelope(bundle=bundle)
        bh = bundle_hash(bundle)
        ch = approval_challenge_hex(env)
        assert bh != ch

    def test_canonical_bytes_no_floats(self) -> None:
        """No float values in canonical bytes."""
        bundle = _make_bundle()
        cb = canonical_bytes(bundle)
        assert b"e+" not in cb  # no scientific notation
        # Verify money is a 2-dp string, not a float.
        parsed = json.loads(cb)
        assert parsed["legs"][0]["amount"] == "50.00"
        assert isinstance(parsed["legs"][0]["amount"], str)

    def test_canonical_bytes_sorted_keys(self) -> None:
        """Keys are sorted at all levels."""
        env = _make_envelope()
        cb = canonical_bytes(env)
        parsed = json.loads(cb)
        assert list(parsed.keys()) == sorted(parsed.keys())
        payload = parsed["payload"]
        assert list(payload.keys()) == sorted(payload.keys())


# ---------------------------------------------------------------------------
# Challenge change tests (parametrized)
# ---------------------------------------------------------------------------


class TestChallengeChanges:
    """Changing any signed field must change the challenge."""

    @pytest.fixture()
    def base_env(self) -> SigningEnvelope:
        return _make_envelope()

    def test_change_amount_changes_challenge(self, base_env: SigningEnvelope) -> None:
        leg2 = _make_leg(amount="51.00")
        bundle2 = _make_bundle(legs=[leg2])
        env2 = _make_envelope(bundle=bundle2)
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_target_id_changes_challenge(self, base_env: SigningEnvelope) -> None:
        leg2 = _make_leg(target_id="acct-joint")
        bundle2 = _make_bundle(legs=[leg2])
        env2 = _make_envelope(bundle=bundle2)
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_leg_order_changes_challenge(self, base_env: SigningEnvelope) -> None:
        leg_a = _make_leg(leg_id="a", amount="10.00")
        leg_b = _make_leg(leg_id="b", amount="20.00", target_id="other")
        bundle_ab = _make_bundle(legs=[leg_a, leg_b])
        bundle_ba = _make_bundle(legs=[leg_b, leg_a])
        env_ab = _make_envelope(bundle=bundle_ab)
        env_ba = _make_envelope(bundle=bundle_ba)
        assert approval_challenge_hex(env_ab) != approval_challenge_hex(env_ba)

    def test_change_operation_changes_challenge(self, base_env: SigningEnvelope) -> None:
        from shared.schemas import MandateChange

        env2 = _make_envelope(
            operation="update_mandate",
            payload=MandateChange(paused=True),
            conversation_id=None,
            conversation_revision=None,
            conversation_hash=None,
        )
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_user_id_changes_challenge(self, base_env: SigningEnvelope) -> None:
        # Also change the bundle owner to match (envelope-payload binding).
        bundle2 = _make_bundle(owner_user_id="user-attacker")
        env2 = _make_envelope(bundle=bundle2, user_id="user-attacker")
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_credential_id_changes_challenge(self, base_env: SigningEnvelope) -> None:
        env2 = _make_envelope(credential_id="cred-002")
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_nonce_changes_challenge(self, base_env: SigningEnvelope) -> None:
        env2 = _make_envelope(nonce="ff" * 32)
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_expires_at_changes_challenge(self, base_env: SigningEnvelope) -> None:
        env2 = _make_envelope(expires_at="2026-10-10T12:00:02.000Z")
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)

    def test_change_conversation_binding_changes_challenge(self, base_env: SigningEnvelope) -> None:
        # Also change the bundle conversation_hash to match (envelope-payload binding).
        bundle2 = _make_bundle(conversation_hash="different_hash_001")
        env2 = _make_envelope(
            bundle=bundle2,
            conversation_hash="different_hash_001",
        )
        assert approval_challenge_hex(base_env) != approval_challenge_hex(env2)
