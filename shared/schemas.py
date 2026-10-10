"""Strict Pydantic schemas for the DCTA shared layer.

All models use ``extra="forbid"`` to reject unknown fields.  Models that
represent frozen/signed data use ``frozen=True``.

Key design decisions:
    - **Money** is represented by the :class:`~shared.money.Money` type
      (pure Python, no DB/IO).  Pydantic validators accept ``Decimal``,
      ``int``, or valid ``str`` and reject ``float``/``bool``.
    - **AmountExpr** is a pre-compilation tagged union.  No Python
      expressions are evaluated.
    - **SigningEnvelope** enforces the pairing between ``operation`` and
      the type of ``payload``.
    - Nonce is 32 random bytes encoded as 64 lowercase hex characters.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from shared.money import Money
from shared.timeutil import from_iso

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REFERENCE_LEN = 140
NONCE_HEX_LEN = 64
ENVELOPE_TTL_SECONDS = 120
MAX_LEGS = 4

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Action(StrEnum):
    """The four MVP financial actions."""

    transfer = "transfer"
    pay_bill = "pay_bill"
    pay_card = "pay_card"
    buy_equity = "buy_equity"


class TargetType(StrEnum):
    """Valid target types for a leg."""

    own_account = "own_account"
    saved_payee = "saved_payee"
    saved_paynow = "saved_paynow"
    biller = "biller"
    owned_card = "owned_card"
    sgx_symbol = "sgx_symbol"


# Valid action -> target combinations.
_VALID_TARGETS: dict[Action, set[TargetType]] = {
    Action.transfer: {
        TargetType.own_account,
        TargetType.saved_payee,
        TargetType.saved_paynow,
    },
    Action.pay_bill: {TargetType.biller},
    Action.pay_card: {TargetType.owned_card},
    Action.buy_equity: {TargetType.sgx_symbol},
}


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Base with ``extra="forbid"``."""

    model_config = ConfigDict(extra="forbid")


class _StrictFrozen(BaseModel):
    """Base with ``extra="forbid"`` and ``frozen=True``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Money Pydantic type
# ---------------------------------------------------------------------------

MoneyField = Annotated[Money, Field(...)]


def _parse_money(v: Any) -> Money:
    """Pydantic before-validator for Money fields."""
    if isinstance(v, Money):
        return v
    return Money(v)


# ---------------------------------------------------------------------------
# Lineage data types (pure data; helpers live in shared/lineage.py)
# ---------------------------------------------------------------------------


class SourceRef(_StrictFrozen):
    """A reference to a source for a field value.

    ``kind`` identifies the source type:

        - ``user_turn``: a span of an authenticated user turn in the
          captured conversation.  Requires ``turn_id``, ``span_start``,
          ``span_end``.
        - ``user_selection``: an explicit user selection from presented
          options (e.g. a chip tap).  Requires ``turn_id``, ``span_start``,
          ``span_end``.
        - ``bank_record``: a server-held bank lookup result
          (account, payee, biller, card, quote).  Requires ``record_id``.
        - ``reference_text``: untrusted free text from a leg's
          ``reference`` field, bill references, memo/label text from a
          bank record, or pasted instructions.  Requires either
          ``turn_id`` + span or ``record_id`` + optional span.
          **Not eligible for critical fields.**
        - ``derived``: a value computed from other sources.  Carries
          parent refs on :class:`Lineage` and inherits taint.

    ``span_start`` / ``span_end`` are character offsets into the
    turn text or record label text.
    """

    kind: Literal[
        "user_turn",
        "bank_record",
        "user_selection",
        "reference_text",
        "derived",
    ]
    turn_id: str | None = None
    record_id: str | None = None
    span_start: int | None = None
    span_end: int | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> SourceRef:
        if self.kind in ("user_turn", "user_selection"):
            if self.turn_id is None:
                raise ValueError(f"{self.kind} requires turn_id")
            if self.span_start is None or self.span_end is None:
                raise ValueError(f"{self.kind} requires span_start and span_end")
        elif self.kind == "bank_record":
            if self.record_id is None:
                raise ValueError("bank_record requires record_id")
        elif self.kind == "reference_text":
            # Requires either turn_id + span or record_id + optional span.
            if self.turn_id is not None:
                if self.span_start is None or self.span_end is None:
                    raise ValueError("reference_text with turn_id requires span_start and span_end")
            elif self.record_id is not None:
                pass  # span is optional for record-based reference text
            else:
                raise ValueError("reference_text requires turn_id+span or record_id")
        return self


class Lineage(_StrictFrozen):
    """Provenance record for a field or leg.

    ``source_refs`` lists the trusted sources.  ``parent_refs`` is
    non-empty when ``source_refs`` contains a ``derived`` entry; it
    carries the full set of parent SourceRefs and inherits taint.

    Trusted code builds Lineage objects; model-supplied lineage is never
    accepted as authoritative (see :func:`shared.lineage.rebuild_from_turns`).
    """

    source_refs: list[SourceRef] = Field(..., min_length=1)
    parent_refs: list[SourceRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Equity terms
# ---------------------------------------------------------------------------


class EquityTerms(_StrictFrozen):
    """Frozen equity purchase terms.

    Validates whole-share arithmetic:
        - ``quantity == floor(budget / unit_price)``
        - ``actual_debit == quantity * unit_price``
        - ``residual_cash == budget - actual_debit``
        - ``quote_expires_at > quoted_at``
        - ``quantity >= 1`` (zero-quantity purchase rejected)
    """

    budget: MoneyField
    quote_id: str = Field(..., min_length=1)
    unit_price: MoneyField
    quoted_at: str
    quote_expires_at: str
    quantity: int = Field(..., ge=1)
    actual_debit: MoneyField
    residual_cash: MoneyField

    @field_validator("quoted_at", "quote_expires_at")
    @classmethod
    def _valid_ts(cls, v: str) -> str:
        from_iso(v)
        return v

    @model_validator(mode="after")
    def _check_arithmetic(self) -> EquityTerms:
        if self.unit_price.value == 0:
            raise ValueError("unit_price must be positive")
        expected_qty = int(self.budget.value // self.unit_price.value)
        if self.quantity != expected_qty:
            raise ValueError(
                f"quantity {self.quantity} != floor(budget/unit_price) = {expected_qty}"
            )
        if self.quantity < 1:
            raise ValueError("quantity must be >= 1")

        expected_debit = self.unit_price.value * self.quantity
        if self.actual_debit.value != expected_debit:
            raise ValueError(
                f"actual_debit {self.actual_debit} != quantity * unit_price = {expected_debit}"
            )

        expected_residual = self.budget.value - self.actual_debit.value
        if self.residual_cash.value != expected_residual:
            raise ValueError(
                f"residual_cash {self.residual_cash} != budget - actual_debit = {expected_residual}"
            )

        quoted = from_iso(self.quoted_at)
        expires = from_iso(self.quote_expires_at)
        if expires <= quoted:
            raise ValueError("quote_expires_at must be after quoted_at")

        return self


# ---------------------------------------------------------------------------
# Leg display metadata (gateway-resolved)
# ---------------------------------------------------------------------------


class DisplayMetadata(_StrictFrozen):
    """Gateway-resolved display metadata for a leg's target.

    Contains human-readable name and masked account suffix, resolved
    server-side from bank records.  Never set from agent/client input.
    """

    display_name: str = Field(..., min_length=1)
    masked_account_suffix: str | None = None


# ---------------------------------------------------------------------------
# Leg
# ---------------------------------------------------------------------------


class Leg(_StrictFrozen):
    """A single financial action within a bundle.

    ``amount`` must be positive.  ``currency`` is always ``"SGD"``.
    ``reference`` is a bounded free-text field (e.g. bill reference),
    capped at :data:`MAX_REFERENCE_LEN`.

    ``equity_terms`` is required iff ``action`` is ``buy_equity``;
    the leg amount must equal ``equity_terms.actual_debit``.

    ``lineage`` records provenance for critical fields (amount,
    target_id, source_account_id).  ``dependencies`` records lineage
    for derived amounts.
    """

    leg_id: str = Field(..., min_length=1)
    action: Action
    source_account_id: str = Field(..., min_length=1)
    target_type: TargetType
    target_id: str = Field(..., min_length=1)
    display_metadata: DisplayMetadata
    amount: MoneyField
    currency: Literal["SGD"] = "SGD"
    reference: str | None = Field(default=None, max_length=MAX_REFERENCE_LEN)
    equity_terms: EquityTerms | None = None
    lineage: Lineage | None = None
    dependencies: list[Lineage] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_leg(self) -> Leg:
        valid_targets = _VALID_TARGETS.get(self.action, set())
        if self.target_type not in valid_targets:
            raise ValueError(
                f"action {self.action.value} requires target in "
                f"{[t.value for t in valid_targets]}; "
                f"got {self.target_type.value}"
            )

        if self.amount.value <= 0:
            raise ValueError("leg amount must be positive (non-zero)")

        if self.action == Action.buy_equity:
            if self.equity_terms is None:
                raise ValueError("equity_terms is required for buy_equity")
            if self.amount.value != self.equity_terms.actual_debit.value:
                raise ValueError("leg amount must equal equity_terms.actual_debit")
        else:
            if self.equity_terms is not None:
                raise ValueError(f"equity_terms is not allowed for action {self.action.value}")

        return self


# ---------------------------------------------------------------------------
# Pre-compilation AmountExpr (tagged union, no eval)
# ---------------------------------------------------------------------------


class AmountLiteral(_Strict):
    """A literal SGD amount."""

    kind: Literal["literal"] = "literal"
    amount: MoneyField

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, v: Any) -> Money:
        return _parse_money(v)

    @model_validator(mode="after")
    def _positive(self) -> AmountLiteral:
        if self.amount.value <= 0:
            raise ValueError("literal amount must be positive")
        return self


class FractionOfLeg(_Strict):
    """A fraction of a preceding leg's amount.

    ``leg_id`` must reference a leg that appears earlier in the bundle.
    ``numerator`` and ``denominator`` are small positive integers with
    ``fraction <= 1`` (i.e. ``numerator <= denominator``).
    """

    kind: Literal["fraction_of_leg"] = "fraction_of_leg"
    leg_id: str = Field(..., min_length=1)
    numerator: int = Field(..., ge=1, le=1000)
    denominator: int = Field(..., ge=1, le=1000)

    @model_validator(mode="after")
    def _fraction_le_one(self) -> FractionOfLeg:
        if self.numerator > self.denominator:
            raise ValueError("fraction must be <= 1 (numerator <= denominator)")
        return self


class RemainingBalance(_Strict):
    """The remaining balance of a named account after preceding legs."""

    kind: Literal["remaining_balance"] = "remaining_balance"
    account_id: str = Field(..., min_length=1)


AmountExpr = Annotated[
    AmountLiteral | FractionOfLeg | RemainingBalance,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Frozen bundle
# ---------------------------------------------------------------------------


class FrozenBundle(_StrictFrozen):
    """An immutable, resolved bundle ready for signing.

    - 1-4 legs, unique ``leg_id``s, order retained.
    - All amounts are resolved (no AmountExpr at this stage).
    - ``conversation_hash`` binds to the captured conversation.
    """

    bundle_id: UUID
    revision: int = Field(..., ge=1)
    owner_user_id: str = Field(..., min_length=1)
    conversation_id: str = Field(..., min_length=1)
    conversation_revision: int = Field(..., ge=1)
    conversation_hash: str = Field(..., min_length=1)
    created_at: str
    legs: list[Leg] = Field(..., min_length=1, max_length=MAX_LEGS)

    @field_validator("created_at")
    @classmethod
    def _valid_created(cls, v: str) -> str:
        from_iso(v)
        return v

    @model_validator(mode="after")
    def _unique_leg_ids(self) -> FrozenBundle:
        ids = [leg.leg_id for leg in self.legs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate leg_id in bundle")
        return self


# ---------------------------------------------------------------------------
# Mandate change payload (minimal, typed; T-07 fills semantics)
# ---------------------------------------------------------------------------


class MandateChange(_StrictFrozen):
    """Minimal typed mandate-change payload for ``update_mandate``.

    T-07 will add full semantics (cooling, mixed-change rejection, etc.).
    For T-01 we only need the shape to be correct and typed.
    """

    per_leg_limit: MoneyField | None = None
    daily_limit: MoneyField | None = None
    paused: bool = False
    allowed_actions: list[Action] = Field(default_factory=list)

    @field_validator("per_leg_limit", "daily_limit", mode="before")
    @classmethod
    def _parse_opt_money(cls, v: Any) -> Any:
        if v is None:
            return None
        return _parse_money(v)


# ---------------------------------------------------------------------------
# Signing envelope
# ---------------------------------------------------------------------------

_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


class SigningEnvelope(_StrictFrozen):
    """The canonical signing envelope for WebAuthn challenge issuance.

    ``operation`` determines the payload type:
        - ``execute_bundle`` -> ``FrozenBundle``
        - ``update_mandate`` -> ``MandateChange``

    Conversation binding (``conversation_id``, ``conversation_revision``,
    ``conversation_hash``) is **required** for ``execute_bundle`` and
    **absent** for ``update_mandate``.

    ``nonce`` is 32 random bytes encoded as 64 lowercase hex characters.

    ``expires_at - issued_at`` must be > 0 and <= 120 seconds.
    """

    schema_version: Literal[1] = 1
    operation: Literal["execute_bundle", "update_mandate"]
    user_id: str = Field(..., min_length=1)
    credential_id: str = Field(..., min_length=1)
    operation_id: str = Field(..., min_length=1)
    payload: FrozenBundle | MandateChange
    conversation_id: str | None = None
    conversation_revision: int | None = None
    conversation_hash: str | None = None
    active_mandate_version: int = Field(..., ge=1)
    nonce: str
    issued_at: str
    expires_at: str

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _valid_ts(cls, v: str) -> str:
        from_iso(v)
        return v

    @field_validator("nonce")
    @classmethod
    def _nonce_hex(cls, v: str) -> str:
        if not _NONCE_RE.match(v):
            raise ValueError(f"nonce must be {NONCE_HEX_LEN} lowercase hex chars; got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_pairing_and_ttl(self) -> SigningEnvelope:
        if self.operation == "execute_bundle":
            if not isinstance(self.payload, FrozenBundle):
                raise ValueError("execute_bundle requires a FrozenBundle payload")
            if (
                self.conversation_id is None
                or self.conversation_revision is None
                or self.conversation_hash is None
            ):
                raise ValueError(
                    "execute_bundle requires conversation_id, "
                    "conversation_revision and conversation_hash"
                )
            # Envelope-payload binding: user and conversation must agree.
            if self.user_id != self.payload.owner_user_id:
                raise ValueError(
                    f"envelope user_id {self.user_id!r} != "
                    f"payload owner_user_id {self.payload.owner_user_id!r}"
                )
            if self.conversation_id != self.payload.conversation_id:
                raise ValueError(
                    f"envelope conversation_id {self.conversation_id!r} != "
                    f"payload conversation_id {self.payload.conversation_id!r}"
                )
            if self.conversation_revision != self.payload.conversation_revision:
                raise ValueError(
                    f"envelope conversation_revision {self.conversation_revision} != "
                    f"payload conversation_revision "
                    f"{self.payload.conversation_revision}"
                )
            if self.conversation_hash != self.payload.conversation_hash:
                raise ValueError(
                    f"envelope conversation_hash {self.conversation_hash!r} != "
                    f"payload conversation_hash "
                    f"{self.payload.conversation_hash!r}"
                )
        elif self.operation == "update_mandate":
            if not isinstance(self.payload, MandateChange):
                raise ValueError("update_mandate requires a MandateChange payload")
            if (
                self.conversation_id is not None
                or self.conversation_revision is not None
                or self.conversation_hash is not None
            ):
                raise ValueError("update_mandate must not carry conversation binding")

        issued = from_iso(self.issued_at)
        expires = from_iso(self.expires_at)
        delta = (expires - issued).total_seconds()
        if delta <= 0:
            raise ValueError("expires_at must be after issued_at")
        if delta > ENVELOPE_TTL_SECONDS:
            raise ValueError(f"envelope TTL {delta}s exceeds {ENVELOPE_TTL_SECONDS}s limit")

        return self


# ---------------------------------------------------------------------------
# AmountExpr resolver (pure function, no I/O)
# ---------------------------------------------------------------------------


def resolve_amount_exprs(
    draft_legs: list[tuple[str, str, AmountExpr | Money]],
    balances: dict[str, Decimal],
) -> list[Money]:
    """Resolve pre-compilation AmountExpr values into concrete Money.

    Each tuple in *draft_legs* is ``(leg_id, source_account_id, expr)``.

    Rules:
        - :class:`AmountLiteral` -> its amount.
        - :class:`FractionOfLeg` -> the referenced leg's resolved amount
          x ``numerator / denominator``, quantized with ``ROUND_DOWN``
          to 2 dp.
        - :class:`RemainingBalance` -> ``balances[account_id]`` minus
          the sum of all **preceding** resolved amounts that debit the
          same account.  Quantized with ``ROUND_DOWN`` to 2 dp.

    Rejects:
        - Forward references (leg_id not yet seen).
        - Missing references (leg_id not in draft).
        - Self-reference (a leg referencing itself).
        - Multi-leg cycles are impossible because fractions may only
          reference already-resolved legs.

    **Rounding rule**: ``ROUND_DOWN`` (truncation toward zero) to 2 dp.
    A fraction like ``1/3`` of ``100.00`` resolves to ``33.33``, never
    ``33.34``.  This is the conservative choice: the user never pays
    more than the exact fraction.
    """
    resolved: list[Money] = []
    resolved_by_leg: dict[str, Money] = {}
    account_debits: dict[str, Decimal] = {}

    for leg_id, source_account_id, expr in draft_legs:
        if isinstance(expr, Money):
            money = expr
        elif isinstance(expr, AmountLiteral):
            money = expr.amount
        elif isinstance(expr, FractionOfLeg):
            ref_id = expr.leg_id
            if ref_id == leg_id:
                raise ValueError(f"leg {leg_id}: self-reference in fraction_of_leg")
            if ref_id not in resolved_by_leg:
                raise ValueError(
                    f"leg {leg_id}: fraction_of_leg references unknown or forward leg {ref_id!r}"
                )
            ref_amount = resolved_by_leg[ref_id].value
            raw = ref_amount * expr.numerator / expr.denominator
            quantized = raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            money = Money(quantized)
        elif isinstance(expr, RemainingBalance):
            acct = expr.account_id
            if acct not in balances:
                raise ValueError(
                    f"leg {leg_id}: remaining_balance references unknown account {acct!r}"
                )
            current_balance = balances[acct]
            already_debited = account_debits.get(acct, Decimal("0"))
            raw = current_balance - already_debited
            if raw < 0:
                raise ValueError(
                    f"leg {leg_id}: remaining balance for account {acct} is negative ({raw})"
                )
            quantized = raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            money = Money(quantized)
        else:
            raise TypeError(f"unsupported expr type: {type(expr).__name__}")

        resolved.append(money)
        resolved_by_leg[leg_id] = money
        account_debits[source_account_id] = (
            account_debits.get(source_account_id, Decimal("0")) + money.value
        )

    return resolved
