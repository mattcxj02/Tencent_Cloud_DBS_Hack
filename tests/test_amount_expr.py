"""AmountExpr resolver tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.money import Money
from shared.schemas import (
    AmountLiteral,
    FractionOfLeg,
    RemainingBalance,
    resolve_amount_exprs,
)


class TestLiteralResolve:
    def test_literal_amount(self) -> None:
        legs = [("leg-1", "acct-a", AmountLiteral(amount=Money("50.00")))]
        result = resolve_amount_exprs(legs, {})
        assert len(result) == 1
        assert result[0].to_str() == "50.00"


class TestFractionOfLeg:
    def test_half_of_preceding_leg(self) -> None:
        legs = [
            ("leg-1", "acct-a", AmountLiteral(amount=Money("100.00"))),
            ("leg-2", "acct-b", FractionOfLeg(leg_id="leg-1", numerator=1, denominator=2)),
        ]
        result = resolve_amount_exprs(legs, {})
        assert result[1].to_str() == "50.00"

    def test_third_rounds_down(self) -> None:
        """1/3 of 100.00 = 33.33 (ROUND_DOWN)."""
        legs = [
            ("leg-1", "acct-a", AmountLiteral(amount=Money("100.00"))),
            ("leg-2", "acct-b", FractionOfLeg(leg_id="leg-1", numerator=1, denominator=3)),
        ]
        result = resolve_amount_exprs(legs, {})
        assert result[1].to_str() == "33.33"

    def test_forward_reference_rejected(self) -> None:
        legs = [
            ("leg-1", "acct-a", FractionOfLeg(leg_id="leg-2", numerator=1, denominator=2)),
            ("leg-2", "acct-b", AmountLiteral(amount=Money("100.00"))),
        ]
        with pytest.raises(ValueError, match="forward|unknown"):
            resolve_amount_exprs(legs, {})

    def test_missing_reference_rejected(self) -> None:
        legs = [
            ("leg-1", "acct-a", FractionOfLeg(leg_id="nonexistent", numerator=1, denominator=2)),
        ]
        with pytest.raises(ValueError, match="forward|unknown"):
            resolve_amount_exprs(legs, {})

    def test_self_reference_rejected(self) -> None:
        legs = [
            ("leg-1", "acct-a", FractionOfLeg(leg_id="leg-1", numerator=1, denominator=2)),
        ]
        with pytest.raises(ValueError, match="self-reference"):
            resolve_amount_exprs(legs, {})

    def test_fraction_gt_one_rejected(self) -> None:
        """numerator > denominator is rejected at the model level."""
        with pytest.raises(Exception, match="fraction"):
            FractionOfLeg(leg_id="leg-1", numerator=3, denominator=2)


class TestRemainingBalance:
    def test_remaining_balance_simple(self) -> None:
        legs = [
            ("leg-1", "acct-a", RemainingBalance(account_id="acct-a")),
        ]
        balances = {"acct-a": Decimal("500.00")}
        result = resolve_amount_exprs(legs, balances)
        assert result[0].to_str() == "500.00"

    def test_remaining_balance_after_preceding_debit(self) -> None:
        """Remaining balance after a preceding leg debited the same account."""
        legs = [
            ("leg-1", "acct-a", AmountLiteral(amount=Money("200.00"))),
            ("leg-2", "acct-a", RemainingBalance(account_id="acct-a")),
        ]
        balances = {"acct-a": Decimal("500.00")}
        result = resolve_amount_exprs(legs, balances)
        assert result[0].to_str() == "200.00"
        assert result[1].to_str() == "300.00"

    def test_remaining_balance_unknown_account_rejected(self) -> None:
        legs = [
            ("leg-1", "acct-a", RemainingBalance(account_id="acct-unknown")),
        ]
        with pytest.raises(ValueError, match="unknown account"):
            resolve_amount_exprs(legs, {"acct-a": Decimal("100.00")})

    def test_remaining_balance_negative_rejected(self) -> None:
        """If debits exceed balance, reject."""
        legs = [
            ("leg-1", "acct-a", AmountLiteral(amount=Money("600.00"))),
            ("leg-2", "acct-a", RemainingBalance(account_id="acct-a")),
        ]
        balances = {"acct-a": Decimal("500.00")}
        with pytest.raises(ValueError, match="negative"):
            resolve_amount_exprs(legs, balances)
