"""SGD money type over ``decimal.Decimal``.

Money is serialized as a two-decimal string (e.g. ``"50.00"``).
Floats are rejected at construction time to avoid binary-representation
errors such as ``0.1 + 0.2``.  The same rules apply to mock equity unit
prices.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

__all__ = ["Money", "MAX_MONEY_SGD"]

#: Hard cap on a single SGD amount in this prototype.
MAX_MONEY_SGD = Decimal("1000000.00")

_STR_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class Money:
    """Immutable SGD money value backed by ``Decimal``.

    Accepts:
        - ``Decimal`` – must have at most 2 fractional digits.
        - ``int`` – normalized to 2 dp.
        - ``str`` – must match ``^\\d+(\\.\\d{1,2})?$``.

    Rejects:
        - ``float`` (including values that arrive via JSON numbers).
        - ``bool`` (which is an ``int`` subclass in Python).
        - ``NaN``, ``Infinity``, negative values.
        - More than 2 fractional digits.
        - Values exceeding :data:`MAX_MONEY_SGD`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any, *, _allow_zero: bool = True) -> None:
        """Create a :class:`Money` instance.

        ``_allow_zero`` is a private escape hatch used by the validator
        tests; production code always allows zero as a valid (zero-amount)
        value for non-amount fields.
        """
        if isinstance(value, bool):
            raise TypeError("bool is not a valid money value")

        if isinstance(value, float):
            raise TypeError("float is not a valid money value; use Decimal or str")

        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int) and not isinstance(value, bool):
            dec = Decimal(value)
        elif isinstance(value, str):
            if not _STR_RE.match(value):
                raise ValueError(f"money string must match ^\\d+(\\.\\d{{1,2}})?$; got {value!r}")
            dec = Decimal(value)
        elif isinstance(value, Money):
            dec = value._value
        else:
            raise TypeError(f"unsupported type for Money: {type(value).__name__}")

        if not dec.is_finite():
            raise ValueError("money must be finite (not NaN or Infinity)")

        if dec < 0:
            raise ValueError("money must be non-negative")

        # Reject more than 2 fractional digits.
        if dec.as_tuple().exponent < -2:
            raise ValueError(
                f"money must have at most 2 decimal places; got exponent {dec.as_tuple().exponent}"
            )

        if dec > MAX_MONEY_SGD:
            raise ValueError(f"money {dec} exceeds the prototype cap of {MAX_MONEY_SGD}")

        if not _allow_zero and dec == 0:
            raise ValueError("money must be positive (non-zero)")

        # Normalize to exactly 2 dp.
        object.__setattr__(self, "_value", dec.quantize(Decimal("0.01")))

    # -- properties ---------------------------------------------------------

    @property
    def value(self) -> Decimal:
        """The underlying ``Decimal`` (2 dp)."""
        return self._value

    # -- serialization ------------------------------------------------------

    def to_str(self) -> str:
        """Serialize as a two-decimal string, e.g. ``"50.00"``."""
        return f"{self._value:.2f}"

    def __str__(self) -> str:
        return self.to_str()

    def __repr__(self) -> str:
        return f"Money({self.to_str()!r})"

    # -- comparison / hashing ----------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._value == other._value
        if isinstance(other, (Decimal, int)) and not isinstance(other, bool):
            return self._value == Decimal(other)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._value < other._value
        if isinstance(other, (Decimal, int)) and not isinstance(other, bool):
            return self._value < Decimal(other)
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._value <= other._value
        if isinstance(other, (Decimal, int)) and not isinstance(other, bool):
            return self._value <= Decimal(other)
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._value > other._value
        if isinstance(other, (Decimal, int)) and not isinstance(other, bool):
            return self._value > Decimal(other)
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._value >= other._value
        if isinstance(other, (Decimal, int)) and not isinstance(other, bool):
            return self._value >= Decimal(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    # -- arithmetic helpers -------------------------------------------------

    def __add__(self, other: Money | Decimal | int) -> Money:
        if isinstance(other, Money):
            other = other._value
        elif isinstance(other, bool):
            raise TypeError("cannot add bool to Money")
        elif not isinstance(other, (Decimal, int)):
            return NotImplemented
        return Money(self._value + Decimal(other))

    def __sub__(self, other: Money | Decimal | int) -> Money:
        if isinstance(other, Money):
            other = other._value
        elif isinstance(other, bool):
            raise TypeError("cannot subtract bool from Money")
        elif not isinstance(other, (Decimal, int)):
            return NotImplemented
        return Money(self._value - Decimal(other))

    def __mul__(self, other: int) -> Money:
        if isinstance(other, bool) or not isinstance(other, int):
            return NotImplemented
        return Money(self._value * other)

    __rmul__ = __mul__

    @classmethod
    def quantize_down(cls, value: Decimal) -> Money:
        """Quantize a ``Decimal`` to 2 dp with ``ROUND_DOWN``.

        Used by the :term:`AmountExpr` resolver to floor fractions.
        """
        from decimal import ROUND_DOWN

        return cls(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    # -- Pydantic v2 integration -------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Tell Pydantic how to validate and serialize Money fields.

        Accepts ``str``, ``int``, or ``Decimal`` input (rejects ``float``
        and ``bool`` at validation time via the Money constructor).
        Serializes as a two-decimal string.
        """
        from pydantic_core import core_schema

        def _validate(v: Any) -> Money:
            if isinstance(v, Money):
                return v
            return Money(v)

        return core_schema.no_info_plain_validator_function(
            _validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda m: m.to_str(),
                return_schema=core_schema.str_schema(),
            ),
        )
