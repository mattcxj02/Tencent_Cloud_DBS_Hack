"""Source lineage construction and validation.

Lineage records where each critical field (amount, target_id,
source_account_id) came from.  Only trusted code calls the construction
helpers in this module.  Model-supplied lineage objects are never
accepted as authoritative; :func:`rebuild_from_turns` reconstructs
lineage from server-held turn records.

Key rules (invariant I7):

    - A critical field may be sourced from ``user_turn`` spans,
      ``user_selection`` spans, or ``bank_record`` lookups.
    - ``reference_text`` (free-text ``reference`` field, bill references,
      memo/label text, pasted instructions) is **not** an eligible
      source for a critical field, regardless of labeling.
    - A ``derived`` value carries the full set of parent refs and
      inherits taint: if any parent is ineligible, the derived value
      is also ineligible.
    - :func:`validate_span` proves that a substring exists at a
      location in a turn; it does **not** prove semantic correctness.
"""

from __future__ import annotations

from shared.schemas import Lineage, SourceRef

__all__ = [
    "CRITICAL_FIELDS",
    "ELIGIBLE_KINDS",
    "build_user_turn_ref",
    "build_bank_record_ref",
    "build_user_selection_ref",
    "build_reference_text_ref",
    "build_derived_lineage",
    "build_lineage",
    "validate_span",
    "validate_critical_field_lineage",
    "rebuild_from_turns",
    "is_eligible_for_critical_field",
]

#: Fields that must be sourced from trusted origins only.
CRITICAL_FIELDS = frozenset({"amount", "target_id", "source_account_id"})

#: Source kinds eligible to supply a critical field.
#: ``reference_text`` and ``derived`` are NOT in this set.
ELIGIBLE_KINDS = frozenset({"user_turn", "user_selection", "bank_record"})


# ---------------------------------------------------------------------------
# Construction helpers (trusted code only)
# ---------------------------------------------------------------------------


def build_user_turn_ref(turn_id: str, span_start: int, span_end: int) -> SourceRef:
    """Build a ``user_turn`` source reference."""
    return SourceRef(
        kind="user_turn",
        turn_id=turn_id,
        span_start=span_start,
        span_end=span_end,
    )


def build_user_selection_ref(turn_id: str, span_start: int, span_end: int) -> SourceRef:
    """Build a ``user_selection`` source reference."""
    return SourceRef(
        kind="user_selection",
        turn_id=turn_id,
        span_start=span_start,
        span_end=span_end,
    )


def build_bank_record_ref(record_id: str) -> SourceRef:
    """Build a ``bank_record`` source reference."""
    return SourceRef(kind="bank_record", record_id=record_id)


def build_reference_text_ref(
    *,
    turn_id: str | None = None,
    span_start: int | None = None,
    span_end: int | None = None,
    record_id: str | None = None,
) -> SourceRef:
    """Build a ``reference_text`` source reference.

    Represents untrusted free text: a leg's ``reference`` field, bill
    references, memo/label text from a bank record, or pasted
    instructions.  Requires either ``turn_id`` + span or ``record_id``
    (+ optional span).

    ``reference_text`` is **not** eligible to source a critical field
    (amount, target_id, source_account_id).
    """
    return SourceRef(
        kind="reference_text",
        turn_id=turn_id,
        record_id=record_id,
        span_start=span_start,
        span_end=span_end,
    )


def build_derived_lineage(parent_refs: list[SourceRef]) -> Lineage:
    """Build a ``derived`` lineage from parent refs.

    The derived value inherits taint from all parents: if any parent
    is ineligible for a critical field, so is the derived value.
    """
    if not parent_refs:
        raise ValueError("derived lineage requires at least one parent ref")
    derived_ref = SourceRef(kind="derived")
    return Lineage(source_refs=[derived_ref], parent_refs=parent_refs)


def build_lineage(source_refs: list[SourceRef]) -> Lineage:
    """Build a simple (non-derived) lineage from trusted source refs."""
    if not source_refs:
        raise ValueError("lineage requires at least one source ref")
    return Lineage(source_refs=source_refs)


# ---------------------------------------------------------------------------
# Span validation (proves location, not meaning)
# ---------------------------------------------------------------------------


def validate_span(turn_text: str, start: int, end: int, expected_substring: str) -> bool:
    """Prove that *expected_substring* exists at ``[start, end)`` in *turn_text*.

    Returns ``True`` if ``turn_text[start:end] == expected_substring``,
    ``False`` otherwise (including out-of-bounds indices).

    **This function proves location only.**  It does not prove that the
    substring means what the agent claims it means.  A valid span
    reference is necessary but not sufficient for a critical field.
    """
    if start < 0 or end < start or end > len(turn_text):
        return False
    return turn_text[start:end] == expected_substring


# ---------------------------------------------------------------------------
# Critical-field eligibility
# ---------------------------------------------------------------------------


def is_eligible_for_critical_field(lineage: Lineage) -> bool:
    """Check whether *lineage* is eligible to source a critical field.

    A non-derived source ref must be in :data:`ELIGIBLE_KINDS`
    (``user_turn``, ``user_selection``, ``bank_record``).
    ``reference_text`` and ``derived`` itself are not eligible.

    A ``derived`` source ref inherits taint from its parents: if any
    parent is ineligible (e.g. ``reference_text`` or ``derived``), the
    derived value is also ineligible.
    """
    for ref in lineage.source_refs:
        if ref.kind == "derived":
            # A derived ref is eligible iff all parents are eligible.
            if not lineage.parent_refs:
                return False
            for parent in lineage.parent_refs:
                if parent.kind not in ELIGIBLE_KINDS:
                    return False
        elif ref.kind not in ELIGIBLE_KINDS:
            return False
    return True


def validate_critical_field_lineage(field_name: str, lineage: Lineage) -> None:
    """Raise ``ValueError`` if *lineage* is not eligible for *field_name*.

    Called when assigning lineage to a critical field.
    """
    if field_name not in CRITICAL_FIELDS:
        return  # non-critical fields have no eligibility requirement

    if not is_eligible_for_critical_field(lineage):
        raise ValueError(
            f"lineage for critical field {field_name!r} contains "
            f"ineligible source kinds; critical fields may only be "
            f"sourced from {sorted(ELIGIBLE_KINDS)}"
        )


# ---------------------------------------------------------------------------
# Rebuild lineage from server-held turns (never trust model lineage)
# ---------------------------------------------------------------------------

# A turn record as held by the gateway conversation store.
TurnRecord = tuple[str, str]  # (turn_id, turn_text)


def rebuild_from_turns(
    field_name: str,
    turn_records: list[TurnRecord],
    expected_substring: str,
    bank_record_id: str | None = None,
) -> Lineage:
    """Rebuild lineage for *field_name* from server-held turn records.

    This function searches the captured turns for *expected_substring*.
    If found, it constructs a verified ``user_turn`` SourceRef.  If
    *bank_record_id* is supplied, it adds a ``bank_record`` ref.

    Model-supplied lineage objects are never accepted; this function
    rebuilds them from authoritative server state.
    """
    if field_name not in CRITICAL_FIELDS:
        raise ValueError(
            f"rebuild_from_turns is only for critical fields; "
            f"{field_name!r} is not in {sorted(CRITICAL_FIELDS)}"
        )

    refs: list[SourceRef] = []

    for turn_id, turn_text in turn_records:
        idx = turn_text.find(expected_substring)
        if idx >= 0:
            refs.append(
                build_user_turn_ref(
                    turn_id,
                    span_start=idx,
                    span_end=idx + len(expected_substring),
                )
            )
            break  # first match is sufficient

    if bank_record_id is not None:
        refs.append(build_bank_record_ref(bank_record_id))

    if not refs:
        raise ValueError(
            f"could not rebuild lineage for {field_name!r}: "
            f"substring {expected_substring!r} not found in any turn"
        )

    lineage = build_lineage(refs)
    validate_critical_field_lineage(field_name, lineage)
    return lineage
