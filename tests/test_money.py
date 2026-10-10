"""Money type tests."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from shared.money import MAX_MONEY_SGD, Money


class _Wrapper(BaseModel):
    """Simple Pydantic model wrapping a Money field for JSON tests."""

    amount: Money

    @classmethod
    def model_validate(cls, obj, **kw):  # type: ignore[override]
        return super().model_validate(obj, **kw)


class TestMoneyAccept:
    def test_accept_int(self) -> None:
        m = Money(50)
        assert m.to_str() == "50.00"

    def test_accept_decimal(self) -> None:
        m = Money(Decimal("15.00"))
        assert m.to_str() == "15.00"

    def test_accept_decimal_int_value(self) -> None:
        m = Money(Decimal("100"))
        assert m.to_str() == "100.00"

    def test_accept_str_no_dp(self) -> None:
        m = Money("50")
        assert m.to_str() == "50.00"

    def test_accept_str_one_dp(self) -> None:
        m = Money("50.5")
        assert m.to_str() == "50.50"

    def test_accept_str_two_dp(self) -> None:
        m = Money("50.50")
        assert m.to_str() == "50.50"

    def test_accept_money_passthrough(self) -> None:
        m1 = Money("10.00")
        m2 = Money(m1)
        assert m2.to_str() == "10.00"


class TestMoneyReject:
    def test_reject_float(self) -> None:
        with pytest.raises(TypeError, match="float"):
            Money(0.1)

    def test_reject_bool(self) -> None:
        with pytest.raises(TypeError, match="bool"):
            Money(True)

    def test_reject_nan(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money(Decimal("NaN"))

    def test_reject_infinity(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money(Decimal("Infinity"))

    def test_reject_negative_infinity(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money(Decimal("-Infinity"))

    def test_reject_more_than_two_dp(self) -> None:
        with pytest.raises(ValueError, match="2 decimal places"):
            Money(Decimal("1.234"))

    def test_reject_more_than_two_dp_string(self) -> None:
        """String with > 2 dp is rejected at regex stage."""
        with pytest.raises(ValueError, match="money string"):
            Money("1.234")

    def test_reject_negative(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Money(Decimal("-5.00"))

    def test_reject_negative_string(self) -> None:
        """Negative string is rejected at regex stage."""
        with pytest.raises(ValueError, match="money string"):
            Money("-5.00")

    def test_reject_negative_decimal(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Money(Decimal("-1.00"))

    def test_reject_absurd_size(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            Money(MAX_MONEY_SGD + Decimal("0.01"))

    def test_reject_float_from_json(self) -> None:
        """A JSON float must be rejected when it arrives as a Money field."""
        data = '{"amount": 0.1}'
        parsed = json.loads(data)
        with pytest.raises((TypeError, ValidationError)):
            _Wrapper.model_validate(parsed)

    def test_reject_float_01_plus_02_from_json(self) -> None:
        """0.1+0.2 style float must be rejected."""
        data = '{"amount": 0.30000000000000004}'
        parsed = json.loads(data)
        with pytest.raises((TypeError, ValidationError)):
            _Wrapper.model_validate(parsed)

    def test_reject_string_with_more_than_two_dp(self) -> None:
        with pytest.raises(ValueError):
            Money("1.234")

    def test_reject_empty_string(self) -> None:
        with pytest.raises(ValueError):
            Money("")

    def test_reject_none(self) -> None:
        with pytest.raises(TypeError):
            Money(None)  # type: ignore[arg-type]


class TestMoneySerialize:
    def test_serialize_two_decimal_string(self) -> None:
        assert Money("50").to_str() == "50.00"
        assert Money("50.5").to_str() == "50.50"
        assert Money("50.50").to_str() == "50.50"

    def test_str_repr(self) -> None:
        m = Money("50.00")
        assert str(m) == "50.00"
        assert repr(m) == "Money('50.00')"

    def test_equality(self) -> None:
        assert Money("50.00") == Money("50.00")
        assert Money("50.00") == Decimal("50.00")
        assert Money("50.00") != Money("51.00")

    def test_hashable(self) -> None:
        assert hash(Money("50.00")) == hash(Money("50.00"))
