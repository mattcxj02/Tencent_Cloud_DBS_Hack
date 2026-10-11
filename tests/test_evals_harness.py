"""T-14 evaluation harness: case-file schema, scoring functions and a fake-model dry run.

The dry run drives the real gateway routes, agent graph, validation coordinator and
validator app with an oracle extractor (the case's hand-written quotes) and a scripted
validator. It proves every golden fixture's expected outcome is reachable through the
deterministic code, and that the runner works end to end without network access. Its
validator numbers are perfect by construction and are never reported as results.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evals.cases import (
    CLARIFY_FIELDS,
    Case,
    CaseFileError,
    WrongDraft,
    coverage_problems,
    load_cases,
)
from evals.fakes import OracleExtractor
from evals.harness import (
    NotConstructible,
    RecordingExtractor,
    case_env,
    create_turns,
    discard,
    mutate_bundle,
    run_agent_once,
)
from evals.run import _dataset_items, main
from evals.scoring import (
    classify_outcome,
    compare_draft,
    draft_label,
    latency_summary,
    percentile,
    usage_totals,
    validator_confusion,
)
from gateway.bank import BankStore
from shared.telemetry import redact

# ── Case file ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases()


def test_golden_set_is_valid_and_covers_the_plan(cases: list[Case]) -> None:
    assert len(cases) >= 30
    assert coverage_problems(cases) == []
    assert {c.category for c in cases} == {
        "ordinary",
        "clarification",
        "multi_intent",
        "adversarial",
    }
    assert all(c.split in ("held_out", "dev_overlap") for c in cases)
    for case in cases:
        if case.expected.outcome == "clarify":
            assert case.expected.field in CLARIFY_FIELDS


def test_expected_records_exist_in_the_synthetic_seed(cases: list[Case], tmp_path: Path) -> None:
    db = str(tmp_path / "seed.db")
    BankStore(db).init_and_seed()
    with sqlite3.connect(db) as conn:
        accounts = {r[0] for r in conn.execute("SELECT account_id FROM account_holders "
                                               "WHERE user_id = 'user-matthew'")}  # fmt: skip
        payees = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT payee_id, kind FROM payees WHERE owner_user_id = 'user-matthew'"
            )
        }
        billers = {r[0] for r in conn.execute("SELECT biller_id FROM billers")}
        cards = {r[0] for r in conn.execute("SELECT card_id FROM cards")}
    targets = {
        "own_account": accounts,
        "saved_payee": {p for p, k in payees.items() if k == "bank"},
        "saved_paynow": {p for p, k in payees.items() if k == "paynow"},
        "biller": billers,
        "owned_card": cards,
        "sgx_symbol": {"D05", "O39", "U11", "Z74"},
    }
    for case in cases:
        legs = list(case.expected.legs) if case.expected.outcome == "draft" else []
        legs += [w.new_leg for w in case.wrong_drafts if w.new_leg is not None]
        for leg in legs:
            assert leg.source_account_id in accounts, case.id
            assert leg.target_id in targets[leg.target_type], case.id
        for w in case.wrong_drafts:
            if w.target_id:
                assert w.target_type is not None
                assert w.target_id in targets[w.target_type], (case.id, w.id)
            if w.source_account_id:
                assert w.source_account_id in accounts, (case.id, w.id)


def _write(tmp_path: Path, case: dict) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n")
    return path


def _base_case() -> dict:
    return json.loads(Path("evals/golden/cases.jsonl").read_text().splitlines()[0])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda c: c["expected"]["legs"][0].update(amount="80"), "2 dp"),
        (lambda c: c["expected"]["legs"][0].update(amount="-1.00"), "positive"),
        (lambda c: c["wrong_drafts"][0].update(leg=3), "does not exist"),
        (lambda c: c["wrong_drafts"][0].pop("amount"), "needs"),
        (lambda c: c.update(split="dev_overlap", overlap_note=None), "overlap"),
        (lambda c: c["turns"].insert(0, {"kind": "answer", "text": "yes"}), "answers before"),
        (lambda c: c["turns"].append({"kind": "edit", "edits": 5, "text": "x"}), "edits a turn"),
        (lambda c: c["turns"].append({"kind": "asr", "text": "x"}), "kind"),
        (lambda c: c.update(unexpected=True), "Extra inputs"),
    ],
)
def test_case_schema_rejects_bad_fixtures(tmp_path: Path, mutation, message: str) -> None:
    case = _base_case()
    mutation(case)
    with pytest.raises(CaseFileError, match=message):
        load_cases(_write(tmp_path, case))


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    line = json.dumps(_base_case())
    path = tmp_path / "cases.jsonl"
    path.write_text(f"{line}\n{line}\n")
    with pytest.raises(CaseFileError, match="duplicate"):
        load_cases(path)


def test_dataset_items_are_synthetic_and_untouched_by_redaction(cases: list[Case]) -> None:
    items = _dataset_items(cases, "dcta-golden-test")
    assert len({i["id"] for i in items}) == len(cases)
    for item in items:
        assert redact(item) == item  # nothing secret-shaped, nothing mangled
        assert "oracle_extraction" not in json.dumps(item)


# ── Scoring ───────────────────────────────────────────────────────────

MUM = {
    "action": "transfer",
    "target_type": "saved_payee",
    "target_id": "payee-mum",
    "source_account_id": "acct-matthew-savings",
    "amount": "50.00",
}
CARD = {
    "action": "pay_card",
    "target_type": "owned_card",
    "target_id": "card-matthew-01",
    "source_account_id": "acct-matthew-current",
    "amount": "642.30",
}


def test_compare_draft_exact_and_decimal_equality() -> None:
    assert compare_draft([MUM], [{**MUM, "amount": "50"}]).exact
    assert compare_draft([MUM, CARD], [MUM, CARD]).exact


@pytest.mark.parametrize(
    ("actual", "field"),
    [
        ([{**MUM, "amount": "500.00"}], "amount"),
        ([{**MUM, "target_id": "payee-ahma"}], "target_id"),
        ([{**MUM, "source_account_id": "acct-matthew-current"}], "source_account_id"),
        ([{**MUM, "action": "pay_bill"}], "action"),
    ],
)
def test_compare_draft_reports_the_wrong_critical_field(actual, field: str) -> None:
    result = compare_draft([MUM], actual)
    assert not result.exact
    assert [(d.leg, d.field) for d in result.field_errors] == [(1, field)]


def test_compare_draft_missing_extra_and_order() -> None:
    missing = compare_draft([MUM, CARD], [MUM])
    assert (missing.exact, missing.missing_legs, missing.extra_legs) == (False, 1, 0)
    extra = compare_draft([MUM], [MUM, CARD])
    assert (extra.exact, extra.missing_legs, extra.extra_legs) == (False, 0, 1)
    swapped = compare_draft([MUM, CARD], [CARD, MUM])
    assert not swapped.exact and swapped.missing_legs == swapped.extra_legs == 0


def test_secondary_fields_do_not_change_the_critical_match() -> None:
    result = compare_draft([{**MUM, "reference": "rent"}], [{**MUM, "reference": None}])
    assert result.exact
    assert [d.field for d in result.secondary_errors] == ["reference"]


def _classify(expected: dict, outcome: dict, safe: bool = False) -> str:
    cls, _ = classify_outcome(
        expected, outcome, clarify_fields=CLARIFY_FIELDS, clarify_is_safe=safe
    )
    return cls


def test_classify_outcome_covers_every_class() -> None:
    draft = {"outcome": "draft", "legs": [MUM]}
    ask = {"outcome": "clarify", "field": "recipient"}
    frozen = {"status": "draft_frozen", "legs": [MUM]}
    wrong = {"status": "draft_frozen", "legs": [{**MUM, "amount": "5.00"}]}
    asked = {"status": "clarify", "reason": "RECIPIENT_AMBIGUOUS"}
    asked_other = {"status": "clarify", "reason": "SOURCE_MISSING"}
    error = {"status": "error", "code": "EXTRACTION_MALFORMED"}
    assert _classify(draft, frozen) == "exact"
    assert _classify(draft, wrong) == "incorrect_draft"
    assert _classify(draft, asked) == "unneeded_clarification"
    assert _classify(draft, asked, safe=True) == "safe_clarification"
    assert _classify(draft, error) == "error"
    assert _classify(ask, asked) == "correct_clarification"
    assert _classify(ask, asked_other) == "other_clarification"
    assert _classify(ask, frozen) == "drafted_instead_of_clarifying"
    assert _classify(ask, error) == "error"
    assert draft_label("exact") == "correct"
    assert draft_label("incorrect_draft") == "incorrect"
    assert draft_label("drafted_instead_of_clarifying") == "should_have_clarified"
    assert draft_label("correct_clarification") is None


def test_validator_confusion_counts_false_accepts_and_rejects() -> None:
    result = validator_confusion(
        [
            ("correct", "MATCH"),
            ("correct", "MISMATCH"),
            ("correct", "UNCERTAIN"),
            ("correct", "ERROR"),
            ("incorrect", "MATCH"),
            ("incorrect", "MISMATCH"),
        ]
    )
    correct, incorrect = result["correct"], result["incorrect"]
    assert correct["n"] == correct["accepted_extractor_only"] == 4
    assert correct["accepted_with_validator"] == 1  # 3 false rejects
    assert correct["blocked_with_validator"] == {"MISMATCH": 1, "UNCERTAIN": 1, "ERROR": 1}
    assert incorrect["accepted_extractor_only"] == 2  # extractor-only accepts both
    assert incorrect["accepted_with_validator"] == 1  # one false accept
    with pytest.raises(ValueError):
        validator_confusion([("correct", "MAYBE")])


def test_percentile_is_nearest_rank_with_sample_count() -> None:
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    values = list(range(1, 11))
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile([30, 10, 20], 50) == 20
    with pytest.raises(ValueError):
        percentile(values, 0)
    assert latency_summary([10, None, 30]) == {"n": 2, "p50_ms": 10, "p95_ms": 30, "max_ms": 30}
    assert latency_summary([])["p50_ms"] is None


def test_usage_totals_never_zero_fill_missing_usage() -> None:
    totals = usage_totals([{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, None])
    assert totals == {
        "calls": 2,
        "calls_with_usage": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


# ── Harness pieces and the end-to-end dry run ─────────────────────────


def test_equity_amount_variant_is_not_constructible(cases: list[Case]) -> None:
    case = next(c for c in cases if c.id == "ord-07-equity")
    llm = RecordingExtractor(OracleExtractor(case.oracle_extraction.model_dump(mode="json")))
    with case_env(case.id) as env:
        create_turns(env, case)
        outcome, _, bundle = run_agent_once(env, llm, None)
    try:
        assert outcome["status"] == "draft_frozen" and bundle is not None
        conn = sqlite3.connect(env.db_path)
        conn.row_factory = sqlite3.Row
        with pytest.raises(NotConstructible):
            mutate_bundle(conn, bundle, WrongDraft(id="w-x", kind="wrong_amount", leg=1,
                                                   amount="10.00"))  # fmt: skip
        source = mutate_bundle(
            conn,
            bundle,
            WrongDraft(
                id="w-y", kind="wrong_source", leg=1, source_account_id="acct-matthew-current"
            ),  # fmt: skip
        )
        assert source.legs[0].source_account_id == "acct-matthew-current"
        assert source.bundle_id != bundle.bundle_id
        conn.close()
    finally:
        discard(env)


def test_fake_dry_run_end_to_end(tmp_path: Path, cases: list[Case]) -> None:
    assert main(["--fake", "--out", str(tmp_path)]) == 0
    results = json.loads((tmp_path / "results.json").read_text())
    meta, summary = results["meta"], results["summary"]
    assert meta["mode"] == "fake"
    assert meta["cases_run"] == len(cases)
    assert meta["telemetry_health"]["state"] == "disabled"  # nothing exported
    assert results["langfuse"] == {}
    every = summary["extraction"]["all"]
    # With a perfect extractor every fixture is reachable through the real code.
    assert every["outcome_correct"] == every["n"] == len(cases)
    assert every["unsafe_drafts"] == 0
    built = [w for r in results["records"] for w in r["wrong_drafts"]]
    assert built and all(w["built"] for w in built)
    assert len(built) == sum(len(c.wrong_drafts) for c in cases)
    assert all(e["met"] for e in summary["policy"]["expectations"])
    assert summary["validator"]["all"]["wrong"]["incorrect"]["n"] == len(built)
    report = (tmp_path / "report.md").read_text()
    assert "not measurements" in report
