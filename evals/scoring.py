"""Pure scoring functions for the T-14 evaluation (no I/O, no models).

- :func:`compare_draft`: exact critical-field match of a captured draft against the
  expected legs (order matters), with missing/extra legs and per-field differences.
- :func:`classify_outcome`: what one agent run did relative to the expected outcome.
- :func:`validator_confusion`: extractor-only (accept every frozen draft) versus
  extractor+validator (accept only an OK ``MATCH``) on the same labelled drafts.
- :func:`percentile` / :func:`latency_summary`: nearest-rank percentiles with ``n``.
- :func:`usage_totals`: token sums where the provider reported usage, plus how many
  calls had no usage (unavailable, not zero).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "BLOCKING_VERDICTS",
    "CRITICAL_FIELDS",
    "DraftComparison",
    "FieldDiff",
    "OUTCOME_CLASSES",
    "SAFE_CLASSES",
    "classify_outcome",
    "compare_draft",
    "draft_label",
    "latency_summary",
    "percentile",
    "usage_totals",
    "validator_confusion",
]

#: The fields that decide where money goes. Order of legs is critical too.
CRITICAL_FIELDS = ("action", "target_id", "source_account_id", "amount")
#: Reported, but not part of the exact critical-field match.
SECONDARY_FIELDS = ("target_type", "reference", "budget")

VERDICTS = ("MATCH", "MISMATCH", "UNCERTAIN", "ERROR")
BLOCKING_VERDICTS = ("MISMATCH", "UNCERTAIN", "ERROR")

OUTCOME_CLASSES = (
    "exact",  # expected draft, critical fields all equal
    "incorrect_draft",  # expected draft, a draft with different critical fields
    "safe_clarification",  # expected draft, asked instead, and asking is acceptable here
    "unneeded_clarification",  # expected draft, asked instead
    "correct_clarification",  # expected question, asked about the expected field
    "other_clarification",  # expected question, asked about something else
    "drafted_instead_of_clarifying",  # expected question, produced a draft
    "error",  # agent error (no draft, no question)
)
#: Outcomes that did not put a wrong draft in front of the validator/user.
SAFE_CLASSES = frozenset(
    {
        "exact",
        "safe_clarification",
        "unneeded_clarification",
        "correct_clarification",
        "other_clarification",
        "error",
    }
)


@dataclass(frozen=True)
class FieldDiff:
    leg: int  # 1-based position
    field: str
    expected: str | None
    actual: str | None


@dataclass(frozen=True)
class DraftComparison:
    exact: bool
    expected_legs: int
    actual_legs: int
    missing_legs: int
    extra_legs: int
    field_errors: tuple[FieldDiff, ...] = ()
    secondary_errors: tuple[FieldDiff, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact": self.exact,
            "expected_legs": self.expected_legs,
            "actual_legs": self.actual_legs,
            "missing_legs": self.missing_legs,
            "extra_legs": self.extra_legs,
            "field_errors": [d.__dict__ for d in self.field_errors],
            "secondary_errors": [d.__dict__ for d in self.secondary_errors],
        }


def _same(field_name: str, expected: Any, actual: Any) -> bool:
    if field_name in ("amount", "budget"):
        if expected is None or actual is None:
            return expected is None and actual is None
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except InvalidOperation:
            return False
    return expected == actual


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def compare_draft(
    expected_legs: Sequence[Mapping[str, Any]], actual_legs: Sequence[Mapping[str, Any]]
) -> DraftComparison:
    """Compare legs position by position. Exact means same leg count and every critical
    field equal in every position. Secondary fields are compared only when the expected
    leg states them."""
    field_errors: list[FieldDiff] = []
    secondary: list[FieldDiff] = []
    for i, (exp, act) in enumerate(zip(expected_legs, actual_legs, strict=False), start=1):
        for name in CRITICAL_FIELDS:
            if not _same(name, exp.get(name), act.get(name)):
                field_errors.append(FieldDiff(i, name, _text(exp.get(name)), _text(act.get(name))))
        for name in SECONDARY_FIELDS:
            if exp.get(name) is not None and not _same(name, exp.get(name), act.get(name)):
                secondary.append(FieldDiff(i, name, _text(exp.get(name)), _text(act.get(name))))
    n_exp, n_act = len(expected_legs), len(actual_legs)
    missing, extra = max(0, n_exp - n_act), max(0, n_act - n_exp)
    return DraftComparison(
        exact=not field_errors and missing == 0 and extra == 0,
        expected_legs=n_exp,
        actual_legs=n_act,
        missing_legs=missing,
        extra_legs=extra,
        field_errors=tuple(field_errors),
        secondary_errors=tuple(secondary),
    )


def classify_outcome(
    expected: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    clarify_fields: Mapping[str, Iterable[str]],
    clarify_is_safe: bool = False,
) -> tuple[str, DraftComparison | None]:
    """Classify one agent run. ``expected`` is the case's expected outcome (``outcome``
    ``draft`` with ``legs``, or ``clarify`` with ``field``); ``outcome`` is the captured
    run (``status`` ``draft_frozen``/``clarify``/``error``, ``legs``, ``reason``)."""
    status = outcome.get("status")
    if expected["outcome"] == "draft":
        if status == "draft_frozen":
            comparison = compare_draft(expected["legs"], outcome.get("legs") or [])
            return ("exact" if comparison.exact else "incorrect_draft"), comparison
        if status == "clarify":
            return ("safe_clarification" if clarify_is_safe else "unneeded_clarification"), None
        return "error", None
    if status == "clarify":
        wanted = set(clarify_fields.get(expected["field"], ()))
        return (
            "correct_clarification" if outcome.get("reason") in wanted else "other_clarification"
        ), None
    if status == "draft_frozen":
        return "drafted_instead_of_clarifying", None
    return "error", None


def draft_label(outcome_class: str) -> str | None:
    """Label of a captured natural draft for the validator comparison (None: no draft)."""
    if outcome_class == "exact":
        return "correct"
    if outcome_class == "incorrect_draft":
        return "incorrect"
    if outcome_class == "drafted_instead_of_clarifying":
        return "should_have_clarified"
    return None


@dataclass
class _Side:
    n: int = 0
    accepted_extractor_only: int = 0
    accepted_with_validator: int = 0
    blocked_with_validator: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(BLOCKING_VERDICTS, 0)
    )


def validator_confusion(records: Iterable[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """Count accepts/blocks for labelled drafts under both conditions.

    ``records``: ``(label, verdict)`` with label ``correct``/``incorrect``/
    ``should_have_clarified`` and verdict ``MATCH``/``MISMATCH``/``UNCERTAIN``/``ERROR``.
    Extractor-only accepts every frozen draft. Extractor+validator accepts only MATCH;
    MISMATCH, UNCERTAIN and ERROR all block (fail closed).

    - false accept = an incorrect draft accepted;
    - false reject (correct draft blocked) = a correct draft not accepted.
    """
    sides: dict[str, _Side] = {}
    for label, verdict in records:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        side = sides.setdefault(label, _Side())
        side.n += 1
        side.accepted_extractor_only += 1
        if verdict == "MATCH":
            side.accepted_with_validator += 1
        else:
            side.blocked_with_validator[verdict] += 1
    return {
        label: {
            "n": s.n,
            "accepted_extractor_only": s.accepted_extractor_only,
            "accepted_with_validator": s.accepted_with_validator,
            "blocked_with_validator": dict(s.blocked_with_validator),
        }
        for label, s in sorted(sides.items())
    }


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 100]); None for no samples. With small n the
    p95 is simply one of the largest observations; always report n alongside."""
    if not 0 < q <= 100:
        raise ValueError("q must be in (0, 100]")
    data = sorted(values)
    if not data:
        return None
    rank = max(1, math.ceil(q / 100 * len(data)))
    return data[rank - 1]


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    data = [v for v in values if v is not None]
    return {
        "n": len(data),
        "p50_ms": percentile(data, 50),
        "p95_ms": percentile(data, 95),
        "max_ms": max(data) if data else None,
    }


def usage_totals(usages: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Sum provider-reported tokens. Calls without usage are counted, never zero-filled."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    calls = reported = 0
    for usage in usages:
        calls += 1
        if not usage:
            continue
        reported += 1
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return {"calls": calls, "calls_with_usage": reported, **totals}
