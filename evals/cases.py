"""Golden case schema, loader and coverage checks (T-14).

``evals/golden/cases.jsonl`` holds one reviewed case per line. Expected outcomes are
fixtures written by hand from the synthetic seed (``ledger/seed.py``), not another
model's judgement. Each case is a fixed conversation snapshot (user turns, app questions,
answers, transcript edits), one expected outcome for a single agent run, optional
deliberately wrong drafts for the validator comparison, and an ``oracle_extraction``: the
words a perfect extractor would quote. The oracle is used only by the ``--fake`` dry run,
to prove the fixtures are reachable through the real deterministic agent and gateway code;
it is never shown to a live model.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "ACTIONS",
    "CASES_PATH",
    "CLARIFY_FIELDS",
    "GOLDEN_VERSION",
    "Case",
    "CaseFileError",
    "ExpectedLeg",
    "coverage_problems",
    "file_sha256",
    "load_cases",
]

GOLDEN_VERSION = "golden-v1"
CASES_PATH = Path(__file__).resolve().parent / "golden" / "cases.jsonl"

ACTIONS = ("transfer", "pay_bill", "pay_card", "buy_equity")
TARGET_TYPES = ("own_account", "saved_payee", "saved_paynow", "biller", "owned_card", "sgx_symbol")

#: Which agent reason codes count as asking about each field (agent/questions.py,
#: agent/workflow.py). Asking about a different field is a clarification, but not the
#: expected one.
CLARIFY_FIELDS: dict[str, frozenset[str]] = {
    "recipient": frozenset(
        {
            "RECIPIENT_AMBIGUOUS",
            "RECIPIENT_NOT_FOUND",
            "RECIPIENT_MISSING",
            "RECIPIENT_UNCITABLE",
            "GATEWAY_TARGET_EVIDENCE",
            "GATEWAY_TARGET_EVIDENCE_AMBIGUOUS",
        }
    ),
    "source": frozenset(
        {
            "SOURCE_MISSING",
            "SOURCE_UNCLEAR",
            "SAME_ACCOUNT",
            "REMAINING_ACCOUNT_UNCLEAR",
            "GATEWAY_SOURCE_EVIDENCE",
            "GATEWAY_SOURCE_EVIDENCE_AMBIGUOUS",
        }
    ),
    "amount": frozenset(
        {
            "AMOUNT_MISSING",
            "AMOUNT_UNCLEAR",
            "AMOUNT_NEEDS_VALUE",
            "AMOUNT_OUT_OF_RANGE",
            "CARD_NOTHING_OUTSTANDING",
            "GATEWAY_AMOUNT_FROM_REFERENCE",
        }
    ),
    "currency": frozenset({"AMOUNT_UNSUPPORTED_CURRENCY"}),
    "dependency": frozenset(
        {
            "RELATIVE_AMBIGUOUS",
            "RELATIVE_NOT_FOUND",
            "RELATIVE_NO_EARLIER",
            "GATEWAY_RELATIVE_REFERENCE_INVALID",
        }
    ),
    "leg_count": frozenset({"TOO_MANY_LEGS"}),
    "action": frozenset({"UNSUPPORTED_ACTION", "NO_INTENT"}),
    "reference": frozenset({"QUOTE_IN_REFERENCE", "REFERENCE_TOO_LONG"}),
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class CaseFileError(ValueError):
    """The case file is malformed; the message names the line and problem."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _money(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"not a decimal amount: {value!r}") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"amount must be positive with at most 2 dp: {value!r}")
    if f"{amount:.2f}" != value:
        raise ValueError(f"amount must be written with exactly 2 dp: {value!r}")
    return value


class Turn(_Model):
    """One conversation turn, created through the real gateway routes in order.

    ``text``: typed user text; ``edit``: transcript edit of turn ``edits`` (0-based index
    into this list); ``question``: an app question recorded through the agent-scoped
    internal route; ``answer``: answers the latest question. There is no ``asr`` kind:
    ASR finals come only from the gateway's own TRTC call on recorded audio, so this
    text-only set cannot (and must not) create them.
    """

    kind: Literal["text", "edit", "question", "answer"]
    text: str = Field(min_length=1, max_length=2000)
    edits: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _edit_target(self) -> Turn:
        if (self.kind == "edit") != (self.edits is not None):
            raise ValueError("'edits' is required for, and only allowed on, edit turns")
        return self


class ExpectedLeg(_Model):
    """Critical fields (action, target, source, amount) plus optional non-critical ones."""

    action: Literal["transfer", "pay_bill", "pay_card", "buy_equity"]
    target_type: Literal[
        "own_account", "saved_payee", "saved_paynow", "biller", "owned_card", "sgx_symbol"
    ]
    target_id: str = Field(min_length=1)
    source_account_id: str = Field(min_length=1)
    amount: str
    reference: str | None = None
    budget: str | None = None

    @field_validator("amount", "budget")
    @classmethod
    def _amounts(cls, v: str | None) -> str | None:
        return None if v is None else _money(v)


class ExpectedDraft(_Model):
    outcome: Literal["draft"]
    legs: list[ExpectedLeg] = Field(min_length=1, max_length=4)


class ExpectedClarify(_Model):
    outcome: Literal["clarify"]
    field: Literal[
        "recipient", "source", "amount", "currency", "dependency", "leg_count", "action",
        "reference",
    ]  # fmt: skip


Expected = Annotated[ExpectedDraft | ExpectedClarify, Field(discriminator="outcome")]


class ExpectedPolicy(_Model):
    decision: Literal["PASS", "STEP_UP", "BLOCK"]
    codes_include: list[str] = Field(default_factory=list)


class WrongDraft(_Model):
    """A deliberately wrong variant of the expected draft (validator false-accept probe).

    Applied to the captured natural draft only when that draft matched the expected
    critical fields exactly; ``leg`` is 1-based (``None`` for ``extra_leg``).
    """

    id: str = Field(pattern=r"^w-[a-z0-9-]{1,40}$")
    kind: Literal["wrong_amount", "wrong_recipient", "wrong_source", "extra_leg", "missing_leg"]
    leg: int | None = Field(default=None, ge=1, le=4)
    amount: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    source_account_id: str | None = None
    new_leg: ExpectedLeg | None = None

    @model_validator(mode="after")
    def _fields_for_kind(self) -> WrongDraft:
        need = {
            "wrong_amount": ("leg", "amount"),
            "wrong_recipient": ("leg", "target_type", "target_id"),
            "wrong_source": ("leg", "source_account_id"),
            "extra_leg": ("new_leg",),
            "missing_leg": ("leg",),
        }[self.kind]
        missing = [name for name in need if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind} needs {missing}")
        if self.amount is not None:
            _money(self.amount)
        if self.target_type is not None and self.target_type not in TARGET_TYPES:
            raise ValueError(f"unknown target_type {self.target_type!r}")
        return self


class OracleIntent(_Model):
    action: Literal["transfer", "pay_bill", "pay_card", "buy_equity", "unsupported"]
    amount_quote: str | None = None
    recipient_quote: str | None = None
    source_quote: str | None = None
    reference_quote: str | None = None
    relative_to_quote: str | None = None
    unclear: list[str] = Field(default_factory=list)


class OracleExtraction(_Model):
    intents: list[OracleIntent]
    unclear: list[str] = Field(default_factory=list)


class Case(_Model):
    id: str
    category: Literal["ordinary", "clarification", "multi_intent", "adversarial"]
    #: ``held_out``: not seen while building or tuning anything. ``dev_overlap``: the same
    #: scenario (not the same text) appears in prompt examples, smoke scripts or unit
    #: tests written before this set.
    split: Literal["held_out", "dev_overlap"]
    overlap_note: str | None = None
    description: str = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1, max_length=12)
    expected: Expected
    #: For adversarial draft cases: asking a question instead is safe (not an exact match).
    clarify_is_safe: bool = False
    expected_policy: ExpectedPolicy | None = None
    wrong_drafts: list[WrongDraft] = Field(default_factory=list)
    oracle_extraction: OracleExtraction

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"bad case id {v!r}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Case:
        user_kinds = {"text", "edit", "answer"}
        if not any(t.kind in user_kinds for t in self.turns):
            raise ValueError("a case needs at least one user turn")
        asked = False
        for i, turn in enumerate(self.turns):
            if turn.kind == "edit":
                assert turn.edits is not None
                if turn.edits >= i or self.turns[turn.edits].kind not in user_kinds:
                    raise ValueError(f"turn {i} edits a turn that is not an earlier user turn")
            if turn.kind == "question":
                asked = True
            if turn.kind == "answer" and not asked:
                raise ValueError(f"turn {i} answers before any app question")
        if self.split == "dev_overlap" and not self.overlap_note:
            raise ValueError("dev_overlap cases must say what they overlap with")
        if self.expected.outcome == "clarify" and self.wrong_drafts:
            raise ValueError("wrong drafts need an expected draft to derive from")
        if self.expected.outcome == "draft":
            n = len(self.expected.legs)
            for wd in self.wrong_drafts:
                if wd.leg is not None and wd.leg > n:
                    raise ValueError(f"{wd.id}: leg {wd.leg} does not exist (draft has {n})")
                if wd.kind == "missing_leg" and n < 2:
                    raise ValueError(f"{wd.id}: cannot drop the only leg")
                if wd.kind == "extra_leg" and n >= 4:
                    raise ValueError(f"{wd.id}: an extra leg would exceed 4 legs")
        ids = [wd.id for wd in self.wrong_drafts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate wrong-draft id")
        if self.clarify_is_safe and self.expected.outcome != "draft":
            raise ValueError("clarify_is_safe only applies to expected drafts")
        return self


def file_sha256(path: Path = CASES_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """Parse and validate every line. Raises :class:`CaseFileError` on the first problem."""
    cases: list[Case] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = Case.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CaseFileError(f"{path.name}:{number}: {exc}") from None
        if case.id in seen:
            raise CaseFileError(f"{path.name}:{number}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise CaseFileError(f"{path.name}: no cases")
    return cases


def coverage_problems(cases: list[Case]) -> list[str]:
    """What the PLAN requires of the set but is missing (empty list = covered)."""
    problems: list[str] = []
    categories = {c.category for c in cases}
    for category in ("ordinary", "clarification", "multi_intent", "adversarial"):
        if category not in categories:
            problems.append(f"no {category} case")
    for action in ACTIONS:
        success = any(
            c.expected.outcome == "draft" and any(leg.action == action for leg in c.expected.legs)
            for c in cases
        )
        failure = any(
            c.expected.outcome == "clarify"
            and any(i.action == action for i in c.oracle_extraction.intents)
            for c in cases
        )
        if not success:
            problems.append(f"no successful {action} case")
        if not failure:
            problems.append(f"no {action} case that must be clarified")
    target_types = {
        leg.target_type for c in cases if c.expected.outcome == "draft" for leg in c.expected.legs
    }
    for needed in ("saved_paynow", "own_account"):
        if needed not in target_types:
            problems.append(f"no {needed} draft")
    leg_counts = {len(c.expected.legs) for c in cases if c.expected.outcome == "draft"}
    for n in (1, 2, 3, 4):
        if n not in leg_counts:
            problems.append(f"no {n}-leg draft")
    if not any(t.kind == "edit" for c in cases for t in c.turns):
        problems.append("no transcript-edit case")
    return problems
