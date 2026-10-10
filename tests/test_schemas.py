"""Schema validation tests: extra fields, currency, legs, combos, equity, TTL."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from shared.money import Money
from shared.schemas import (
    Action,
    DisplayMetadata,
    EquityTerms,
    Leg,
    MandateChange,
    SigningEnvelope,
    TargetType,
)
from tests.test_canonical_golden import (
    FIXED_CONVERSATION_HASH,
    FIXED_CONVERSATION_ID,
    FIXED_CREDENTIAL_ID,
    FIXED_EXPIRES_AT,
    FIXED_ISSUED_AT,
    FIXED_MANDATE_VERSION,
    FIXED_NONCE,
    FIXED_OPERATION_ID,
    FIXED_USER_ID,
    _make_bundle,
    _make_leg,
)

# ---------------------------------------------------------------------------
# Extra fields rejected
# ---------------------------------------------------------------------------


class TestExtraFieldsRejected:
    def test_leg_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            Leg(
                leg_id="leg-1",
                action=Action.transfer,
                source_account_id="acct-savings",
                target_type=TargetType.own_account,
                target_id="acct-current",
                display_metadata=DisplayMetadata(
                    display_name="Current",
                    masked_account_suffix="1234",
                ),
                amount=Money("50.00"),
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_envelope_rejects_extra_field(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
                bad_field="no",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


class TestCurrency:
    def test_currency_must_be_sgd(self) -> None:
        with pytest.raises(ValidationError):
            Leg(
                leg_id="leg-1",
                action=Action.transfer,
                source_account_id="acct-savings",
                target_type=TargetType.own_account,
                target_id="acct-current",
                display_metadata=DisplayMetadata(
                    display_name="Current",
                    masked_account_suffix="1234",
                ),
                amount=Money("50.00"),
                currency="USD",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Leg count
# ---------------------------------------------------------------------------


class TestLegCount:
    def test_zero_legs_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(legs=[])

    def test_five_legs_rejected(self) -> None:
        legs = [_make_leg(leg_id=f"leg-{i}", target_id=f"acct-{i}") for i in range(5)]
        with pytest.raises(ValidationError):
            _make_bundle(legs=legs)

    def test_four_legs_accepted(self) -> None:
        legs = [_make_leg(leg_id=f"leg-{i}", target_id=f"acct-{i}") for i in range(4)]
        bundle = _make_bundle(legs=legs)
        assert len(bundle.legs) == 4


# ---------------------------------------------------------------------------
# Duplicate leg_ids
# ---------------------------------------------------------------------------


class TestDuplicateLegIds:
    def test_duplicate_leg_ids_rejected(self) -> None:
        legs = [
            _make_leg(leg_id="dup", target_id="acct-a"),
            _make_leg(leg_id="dup", target_id="acct-b"),
        ]
        with pytest.raises(ValidationError, match="duplicate"):
            _make_bundle(legs=legs)


# ---------------------------------------------------------------------------
# Action/target combinations
# ---------------------------------------------------------------------------


class TestActionTargetCombos:
    @pytest.mark.parametrize(
        ("action", "target_type"),
        [
            (Action.transfer, TargetType.biller),
            (Action.transfer, TargetType.owned_card),
            (Action.transfer, TargetType.sgx_symbol),
            (Action.pay_bill, TargetType.own_account),
            (Action.pay_bill, TargetType.saved_payee),
            (Action.pay_bill, TargetType.owned_card),
            (Action.pay_bill, TargetType.sgx_symbol),
            (Action.pay_card, TargetType.own_account),
            (Action.pay_card, TargetType.saved_payee),
            (Action.pay_card, TargetType.biller),
            (Action.pay_card, TargetType.sgx_symbol),
            (Action.buy_equity, TargetType.own_account),
            (Action.buy_equity, TargetType.saved_payee),
            (Action.buy_equity, TargetType.biller),
            (Action.buy_equity, TargetType.owned_card),
        ],
    )
    def test_invalid_combo_rejected(self, action: Action, target_type: TargetType) -> None:
        with pytest.raises(ValidationError, match="requires target"):
            _make_leg(action=action, target_type=target_type, target_id="x")

    @pytest.mark.parametrize(
        ("action", "target_type"),
        [
            (Action.transfer, TargetType.own_account),
            (Action.transfer, TargetType.saved_payee),
            (Action.transfer, TargetType.saved_paynow),
            (Action.pay_bill, TargetType.biller),
            (Action.pay_card, TargetType.owned_card),
            (Action.buy_equity, TargetType.sgx_symbol),
        ],
    )
    def test_valid_combo_accepted(self, action: Action, target_type: TargetType) -> None:
        leg = _make_leg(action=action, target_type=target_type, target_id="x")
        assert leg.action == action


# ---------------------------------------------------------------------------
# Equity arithmetic
# ---------------------------------------------------------------------------


class TestEquityArithmetic:
    def _make_equity_terms(
        self,
        budget: str = "1000.00",
        unit_price: str = "3.50",
        quote_id: str = "Q-001",
        quoted_at: str = "2026-10-10T10:00:00.000Z",
        quote_expires_at: str = "2026-10-10T10:05:00.000Z",
        quantity: int | None = None,
        actual_debit: str | None = None,
        residual_cash: str | None = None,
    ) -> dict[str, Any]:
        b = Money(budget)
        up = Money(unit_price)
        if quantity is None:
            quantity = int(b.value // up.value)
        if actual_debit is None:
            actual_debit = Money(str(up.value * quantity))
        if residual_cash is None:
            residual_cash = Money(str(b.value - actual_debit.value))
        return {
            "budget": b,
            "quote_id": quote_id,
            "unit_price": up,
            "quoted_at": quoted_at,
            "quote_expires_at": quote_expires_at,
            "quantity": quantity,
            "actual_debit": actual_debit,
            "residual_cash": residual_cash,
        }

    def test_valid_equity_terms(self) -> None:
        terms = EquityTerms(**self._make_equity_terms())
        assert terms.quantity == 285  # floor(1000/3.50) = 285
        assert terms.actual_debit.to_str() == "997.50"
        assert terms.residual_cash.to_str() == "2.50"

    def test_wrong_quantity_rejected(self) -> None:
        d = self._make_equity_terms()
        d["quantity"] = 999
        with pytest.raises(ValidationError, match="quantity"):
            EquityTerms(**d)

    def test_wrong_actual_debit_rejected(self) -> None:
        d = self._make_equity_terms()
        d["actual_debit"] = Money("999.99")
        with pytest.raises(ValidationError, match="actual_debit"):
            EquityTerms(**d)

    def test_wrong_residual_cash_rejected(self) -> None:
        d = self._make_equity_terms()
        d["residual_cash"] = Money("1.00")
        with pytest.raises(ValidationError, match="residual_cash"):
            EquityTerms(**d)

    def test_expiry_before_quoted_rejected(self) -> None:
        d = self._make_equity_terms(
            quoted_at="2026-10-10T10:05:00.000Z",
            quote_expires_at="2026-10-10T10:00:00.000Z",
        )
        with pytest.raises(ValidationError, match="after"):
            EquityTerms(**d)

    def test_zero_quantity_rejected(self) -> None:
        """Budget < unit_price => quantity 0, rejected."""
        d = self._make_equity_terms(budget="2.00", unit_price="3.50")
        with pytest.raises(ValidationError):
            EquityTerms(**d)

    def test_buy_equity_requires_terms(self) -> None:
        """buy_equity without equity_terms is rejected by Leg validator."""
        with pytest.raises(ValidationError, match="equity_terms is required"):
            Leg(
                leg_id="leg-1",
                action=Action.buy_equity,
                source_account_id="acct-savings",
                target_type=TargetType.sgx_symbol,
                target_id="D05",
                display_metadata=DisplayMetadata(
                    display_name="D05",
                    masked_account_suffix=None,
                ),
                amount=Money("50.00"),
            )

    def test_non_equity_rejects_terms(self) -> None:
        terms = EquityTerms(**self._make_equity_terms())
        with pytest.raises(ValidationError, match="not allowed"):
            _make_leg(
                action=Action.transfer,
                target_type=TargetType.own_account,
                equity_terms=terms,
            )


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTL:
    def test_ttl_over_120_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError, match="exceeds"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at="2026-10-10T12:00:00.000Z",
                expires_at="2026-10-10T12:02:01.000Z",  # 121s
            )

    def test_ttl_exactly_120_accepted(self) -> None:
        bundle = _make_bundle()
        env = SigningEnvelope(
            schema_version=1,
            operation="execute_bundle",
            user_id=FIXED_USER_ID,
            credential_id=FIXED_CREDENTIAL_ID,
            operation_id=FIXED_OPERATION_ID,
            payload=bundle,
            conversation_id=FIXED_CONVERSATION_ID,
            conversation_revision=1,
            conversation_hash=FIXED_CONVERSATION_HASH,
            active_mandate_version=FIXED_MANDATE_VERSION,
            nonce=FIXED_NONCE,
            issued_at="2026-10-10T12:00:00.000Z",
            expires_at="2026-10-10T12:02:00.000Z",  # exactly 120s
        )
        assert env.expires_at.endswith("00.000Z")

    def test_negative_ttl_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError, match="after"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at="2026-10-10T12:00:01.000Z",
                expires_at="2026-10-10T12:00:00.000Z",
            )


# ---------------------------------------------------------------------------
# Operation / payload mismatch
# ---------------------------------------------------------------------------


class TestOperationPayloadMismatch:
    def test_execute_bundle_with_mandate_change_rejected(self) -> None:
        mc = MandateChange(paused=True)
        with pytest.raises(ValidationError, match="requires a FrozenBundle"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=mc,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_update_mandate_with_bundle_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError, match="requires a MandateChange"):
            SigningEnvelope(
                schema_version=1,
                operation="update_mandate",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=None,
                conversation_revision=None,
                conversation_hash=None,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_execute_bundle_missing_conversation_binding_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError, match="requires conversation"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=None,
                conversation_revision=None,
                conversation_hash=None,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_update_mandate_with_conversation_binding_rejected(self) -> None:
        mc = MandateChange(paused=True)
        with pytest.raises(ValidationError, match="must not carry"):
            SigningEnvelope(
                schema_version=1,
                operation="update_mandate",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=mc,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )


# ---------------------------------------------------------------------------
# Envelope-payload binding (user and conversation must agree)
# ---------------------------------------------------------------------------


class TestEnvelopePayloadBinding:
    """For execute_bundle, envelope user_id and conversation binding must
    match the FrozenBundle payload's owner_user_id and conversation fields."""

    def test_user_mismatch_rejected(self) -> None:
        bundle = _make_bundle(owner_user_id="user-matthew")
        with pytest.raises(ValidationError, match="user_id"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id="user-attacker",
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_conversation_id_mismatch_rejected(self) -> None:
        bundle = _make_bundle(conversation_id="conv-real")
        with pytest.raises(ValidationError, match="conversation_id"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id="conv-different",
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_conversation_revision_mismatch_rejected(self) -> None:
        bundle = _make_bundle(conversation_revision=1)
        with pytest.raises(ValidationError, match="conversation_revision"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=2,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_conversation_hash_mismatch_rejected(self) -> None:
        bundle = _make_bundle(conversation_hash="hash-real")
        with pytest.raises(ValidationError, match="conversation_hash"):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash="hash-different",
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce=FIXED_NONCE,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_matching_binding_accepted(self) -> None:
        """When envelope and payload agree on user and conversation, it passes."""
        bundle = _make_bundle()
        env = SigningEnvelope(
            schema_version=1,
            operation="execute_bundle",
            user_id=FIXED_USER_ID,
            credential_id=FIXED_CREDENTIAL_ID,
            operation_id=FIXED_OPERATION_ID,
            payload=bundle,
            conversation_id=FIXED_CONVERSATION_ID,
            conversation_revision=1,
            conversation_hash=FIXED_CONVERSATION_HASH,
            active_mandate_version=FIXED_MANDATE_VERSION,
            nonce=FIXED_NONCE,
            issued_at=FIXED_ISSUED_AT,
            expires_at=FIXED_EXPIRES_AT,
        )
        assert env.user_id == env.payload.owner_user_id  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Nonce format
# ---------------------------------------------------------------------------


class TestNonce:
    def test_short_nonce_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce="abc",
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )

    def test_uppercase_nonce_rejected(self) -> None:
        bundle = _make_bundle()
        with pytest.raises(ValidationError):
            SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=FIXED_USER_ID,
                credential_id=FIXED_CREDENTIAL_ID,
                operation_id=FIXED_OPERATION_ID,
                payload=bundle,
                conversation_id=FIXED_CONVERSATION_ID,
                conversation_revision=1,
                conversation_hash=FIXED_CONVERSATION_HASH,
                active_mandate_version=FIXED_MANDATE_VERSION,
                nonce="FF" * 32,
                issued_at=FIXED_ISSUED_AT,
                expires_at=FIXED_EXPIRES_AT,
            )
