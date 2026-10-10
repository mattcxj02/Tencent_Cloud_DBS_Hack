"""Lineage tests: critical-field sourcing, laundering, span validation.

Exercises invariant I7: untrusted free-text sources (``reference_text``)
cannot back a critical field (amount, target_id, source_account_id),
neither directly nor by laundering through ``derived``.
"""

from __future__ import annotations

import pytest

from shared.lineage import (
    build_bank_record_ref,
    build_derived_lineage,
    build_reference_text_ref,
    build_user_turn_ref,
    is_eligible_for_critical_field,
    rebuild_from_turns,
    validate_critical_field_lineage,
    validate_span,
)
from shared.schemas import Lineage, SourceRef

# ---------------------------------------------------------------------------
# Eligible sources accepted
# ---------------------------------------------------------------------------


class TestCriticalFieldSourcing:
    def test_user_turn_amount_accepted(self) -> None:
        """A user_turn source is eligible for a critical field."""
        lineage = Lineage(
            source_refs=[build_user_turn_ref("turn-1", 0, 5)],
        )
        validate_critical_field_lineage("amount", lineage)

    def test_bank_record_target_id_accepted(self) -> None:
        """A bank_record source is eligible."""
        lineage = Lineage(source_refs=[build_bank_record_ref("record-1")])
        validate_critical_field_lineage("target_id", lineage)

    def test_user_selection_accepted(self) -> None:
        """A user_selection source is eligible."""
        lineage = Lineage(
            source_refs=[
                SourceRef(
                    kind="user_selection",
                    turn_id="turn-1",
                    span_start=10,
                    span_end=15,
                )
            ]
        )
        validate_critical_field_lineage("source_account_id", lineage)

    def test_derived_with_all_eligible_parents_accepted(self) -> None:
        """A derived value whose parents are all eligible is accepted."""
        lineage = build_derived_lineage(
            parent_refs=[
                build_user_turn_ref("turn-1", 0, 5),
                build_bank_record_ref("record-1"),
            ]
        )
        validate_critical_field_lineage("amount", lineage)


# ---------------------------------------------------------------------------
# reference_text rejected for critical fields
# ---------------------------------------------------------------------------


class TestReferenceTextRejected:
    def test_reference_text_amount_rejected(self) -> None:
        """A reference_text source cannot back amount."""
        lineage = Lineage(
            source_refs=[build_reference_text_ref(turn_id="turn-1", span_start=0, span_end=3)]
        )
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("amount", lineage)

    def test_reference_text_target_id_rejected(self) -> None:
        """A reference_text source cannot back target_id."""
        lineage = Lineage(source_refs=[build_reference_text_ref(record_id="record-1")])
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("target_id", lineage)

    def test_reference_text_source_account_id_rejected(self) -> None:
        """A reference_text source cannot back source_account_id."""
        lineage = Lineage(
            source_refs=[build_reference_text_ref(turn_id="turn-1", span_start=0, span_end=3)]
        )
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("source_account_id", lineage)


# ---------------------------------------------------------------------------
# Laundering through derived
# ---------------------------------------------------------------------------


class TestLaundering:
    def test_reference_text_laundered_through_derived_rejected(self) -> None:
        """Laundering a reference_text source through 'derived' must fail.

        A derived value whose only parent is reference_text inherits
        taint and is rejected for a critical field.
        """
        ref_text = build_reference_text_ref(turn_id="turn-1", span_start=0, span_end=3)
        lineage = build_derived_lineage(parent_refs=[ref_text])
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("amount", lineage)

    def test_mixed_eligible_and_reference_text_rejected(self) -> None:
        """Taint survives mixing: one eligible + one reference_text parent."""
        good = build_user_turn_ref("turn-1", 0, 5)
        bad = build_reference_text_ref(record_id="record-1")
        lineage = build_derived_lineage(parent_refs=[good, bad])
        assert not is_eligible_for_critical_field(lineage)

    def test_bare_derived_with_no_parents_rejected(self) -> None:
        """A bare derived ref with no parents is rejected (separate case)."""
        lineage = Lineage(source_refs=[SourceRef(kind="derived")])
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("amount", lineage)

    def test_derived_laundered_through_derived_rejected(self) -> None:
        """A derived parent (itself ineligible) taints the chain."""
        bad_parent = SourceRef(kind="derived")
        lineage = build_derived_lineage(parent_refs=[bad_parent])
        with pytest.raises(ValueError, match="ineligible"):
            validate_critical_field_lineage("amount", lineage)


# ---------------------------------------------------------------------------
# Span validation (proves location, not meaning)
# ---------------------------------------------------------------------------


class TestSpanValidation:
    def test_valid_span_true(self) -> None:
        """validate_span returns True for a correct location."""
        text = "transfer 50 to John"
        assert validate_span(text, 9, 11, "50") is True

    def test_wrong_substring_false(self) -> None:
        """validate_span returns False for a wrong substring."""
        text = "transfer 50 to John"
        assert validate_span(text, 9, 11, "60") is False

    def test_out_of_bounds_false(self) -> None:
        """validate_span returns False for out-of-bounds indices."""
        text = "transfer 50"
        assert validate_span(text, 5, 100, "50") is False

    def test_negative_start_false(self) -> None:
        text = "transfer 50"
        assert validate_span(text, -1, 3, "x") is False

    def test_empty_text(self) -> None:
        assert validate_span("", 0, 0, "") is True
        assert validate_span("", 0, 1, "x") is False


# ---------------------------------------------------------------------------
# Rebuild from turns (never trust model lineage)
# ---------------------------------------------------------------------------


class TestRebuildFromTurns:
    def test_rebuild_finds_substring(self) -> None:
        turns = [("turn-1", "transfer 50 to John")]
        lineage = rebuild_from_turns("amount", turns, "50")
        assert len(lineage.source_refs) >= 1
        assert lineage.source_refs[0].kind == "user_turn"
        assert lineage.source_refs[0].turn_id == "turn-1"
        assert lineage.source_refs[0].span_start == 9
        assert lineage.source_refs[0].span_end == 11

    def test_rebuild_with_bank_record(self) -> None:
        turns = [("turn-1", "pay John Doe")]
        lineage = rebuild_from_turns("target_id", turns, "John Doe", bank_record_id="record-1")
        assert len(lineage.source_refs) == 2
        assert lineage.source_refs[1].kind == "bank_record"

    def test_rebuild_not_found_raises(self) -> None:
        turns = [("turn-1", "transfer 50")]
        with pytest.raises(ValueError, match="not found"):
            rebuild_from_turns("amount", turns, "999")

    def test_rebuild_non_critical_field_raises(self) -> None:
        turns = [("turn-1", "some text")]
        with pytest.raises(ValueError, match="only for critical"):
            rebuild_from_turns("reference", turns, "some text")
