"""Frozen arithmetic evidence, stale MATCH invalidation and exact-money contracts."""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.clients.validator import ValidationRequest, request_problem, validation_input_hash
from tests.t05_support import freeze, new_conversation, say
from tests.t10_support import V, ok_reply, v_fixture  # noqa: F401
from validator.app import _trace_input
from validator.prompt import model_view


def _derived(v: V, *, fraction: bool = False) -> str:
    v.sql("UPDATE accounts SET balance_cents = 100015 WHERE account_id = 'acct-matthew-savings'")
    conv = new_conversation(v.env)
    words = "half of that" if fraction else "the rest"
    turn = say(v.env, conv, f"Send 100.10 to Mum from savings then {words} to Ah Ma from savings")
    first = V.leg_body(
        turn,
        {
            "target_id": "payee-mum",
            "amount": "100.10",
            "amount_words": "100.10",
            "target_words": "Mum",
        },
    )
    second = V.leg_body(
        turn,
        {
            "target_id": "payee-ahma",
            "amount": "0.01",
            "amount_words": words,
            "target_words": "Ah Ma",
        },
        2,
    )
    second["amount_expr"] = (
        {"kind": "fraction_of_leg", "leg_id": "leg-1", "numerator": 1, "denominator": 2}
        if fraction
        else {"kind": "remaining_balance", "account_id": "acct-matthew-savings"}
    )
    response = freeze(v.env, conv, [first, second])
    assert response.status_code == 200, response.text
    return response.json()["bundle_id"]


@pytest.mark.parametrize("fraction", [False, True])
def test_gateway_supplies_frozen_exact_calculation_inputs(v: V, fraction: bool) -> None:
    bid = _derived(v, fraction=fraction)
    v.use_transport(ok_reply)
    assert v.validated(bid)["eligible_for_confirmation"]
    request = ValidationRequest.model_validate_json(v.sent[0].content)
    data = model_view(request)["calculation_evidence"]
    assert len(data) == 1
    item = data[0]
    assert item["leg_id"] == "leg-2"
    assert item["result"] == ("50.05" if fraction else "900.05")
    assert item["dependencies"] == [
        {
            "leg_id": "leg-1",
            "source_account_id": "acct-matthew-savings",
            "amount": "100.10",
        }
    ]
    if fraction:
        assert item["balance_snapshot"] is None and item["preceding_debits"] == []
    else:
        assert item["balance_snapshot"] == "1000.15"
        assert item["preceding_debits"] == item["dependencies"]
    assert request_problem(request) is None
    assert "acct-matthew-savings" not in json.dumps(_trace_input(request)["calculation_evidence"])
    assert v.validated(bid)["replayed"] and len(v.sent) == 1


def test_changed_balance_requires_redraft_even_before_validation(v: V) -> None:
    bid = _derived(v)
    v.sql("UPDATE accounts SET balance_cents = 100016 WHERE account_id = 'acct-matthew-savings'")
    v.use_transport(ok_reply)
    response = v.validate(bid)
    assert response.status_code == 409
    assert response.json()["detail"] == "CALCULATION_BALANCE_CHANGED"
    assert not v.sent


def test_balance_change_invalidates_match_and_signed_approval(v: V) -> None:
    bid = _derived(v)
    v.validated(bid)
    issued = v.issue(bid)
    assert issued.status_code == 200
    v.sql("UPDATE accounts SET balance_cents = 200015 WHERE account_id = 'acct-matthew-savings'")
    assert not v.passed(bid)
    assert v.latest(bid).json()["eligible_for_confirmation"] is False
    assert v.issue(bid).status_code == 409
    response = v.approve(issued.json())
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"
    assert response.json()["reason_codes"] == ["SEMANTIC_VALIDATION_REQUIRED"]
    assert not v.q("SELECT * FROM receipts")


def test_balance_change_during_call_supersedes_result(v: V) -> None:
    bid = _derived(v)

    def change_balance(request):
        v.sql(
            "UPDATE accounts SET balance_cents = 100016 WHERE account_id = 'acct-matthew-savings'"
        )
        return ok_reply(request)

    v.use_transport(change_balance)
    result = v.validated(bid)
    assert result["state"] == "SUPERSEDED" and not result["eligible_for_confirmation"]


@pytest.mark.parametrize(
    "field", ["balance_snapshot", "dependencies", "preceding_debits", "result"]
)
def test_altered_calculation_input_changes_hash_and_cannot_validate(v: V, field: str) -> None:
    bid = _derived(v)
    v.use_transport(ok_reply)
    v.validated(bid)
    original = ValidationRequest.model_validate_json(v.sent[0].content)
    data = original.model_dump(mode="json")
    data["calculation_evidence"][0][field] = (
        [] if "debits" in field or field == "dependencies" else "999.00"
    )
    changed = ValidationRequest.model_validate(data)
    assert validation_input_hash(changed) != validation_input_hash(original)
    assert request_problem(changed) == "INVALID_REQUEST"
    v.sql(
        "UPDATE draft_calculations SET evidence_json = ? WHERE bundle_id = ?",
        (
            json.dumps(data["calculation_evidence"]),
            bid,
        ),
    )
    assert not v.passed(bid)


def test_wrong_derived_draft_cannot_reuse_evidence(v: V) -> None:
    bid = _derived(v)
    v.use_transport(ok_reply)
    v.validated(bid)
    request = ValidationRequest.model_validate_json(v.sent[0].content)
    data = request.model_dump(mode="json")
    data["bundle"]["legs"][1]["amount"] = "1000.15"
    changed = ValidationRequest.model_validate(data)
    assert request_problem(changed) == "INVALID_REQUEST"
    assert not v.passed(bid, changed.bundle)


def test_changed_evidence_with_valid_digest_cannot_reuse_match(v: V) -> None:
    bid = _derived(v, fraction=True)
    v.use_transport(ok_reply)
    v.validated(bid)
    evidence = json.loads(v.sent[0].content)["calculation_evidence"]
    evidence[0]["expression"]["denominator"] = 3
    raw = json.dumps(evidence).encode()
    v.sql(
        "UPDATE draft_calculations SET evidence_json = ?, evidence_hash = ? WHERE bundle_id = ?",
        (raw.decode(), hashlib.sha256(raw).hexdigest(), bid),
    )
    assert not v.passed(bid)


def test_pre_upgrade_derived_draft_requires_fresh_freeze(v: V) -> None:
    bid = _derived(v)
    v.sql("DELETE FROM draft_calculations WHERE bundle_id = ?", (bid,))
    v.use_transport(ok_reply)
    response = v.validate(bid)
    assert response.status_code == 409
    assert response.json()["detail"] == "CALCULATION_EVIDENCE_MISSING"
    assert not v.sent


@pytest.mark.parametrize("bad", [1.25, "NaN", "Infinity", "1.001"])
def test_calculation_evidence_rejects_inexact_money(v: V, bad) -> None:
    bid = _derived(v)
    v.use_transport(ok_reply)
    v.validated(bid)
    data = json.loads(v.sent[0].content)
    data["calculation_evidence"][0]["balance_snapshot"] = bad
    with pytest.raises((ValueError, TypeError)):
        ValidationRequest.model_validate(data)


def test_literal_match_does_not_depend_on_unused_balances(v: V) -> None:
    _, bid = v.mum()
    v.validated(bid)
    v.sql("UPDATE accounts SET balance_cents = 999999 WHERE account_id = 'acct-joint-01'")
    assert v.passed(bid)


def test_ordered_preceding_debits_and_fraction_of_remainder(v: V) -> None:
    v.sql("UPDATE accounts SET balance_cents = 100000 WHERE account_id = 'acct-matthew-savings'")
    conv = new_conversation(v.env)
    turn = say(
        v.env,
        conv,
        "Send 100 to Mum, 40 to John Tan, the rest to Ah Ma, then half of that to Mum from savings",
    )
    specs = [
        {
            "target_id": "payee-mum",
            "amount": "100.00",
            "amount_words": "100",
            "target_words": "Mum",
        },
        {
            "target_id": "payee-johntan",
            "amount": "40.00",
            "amount_words": "40",
            "target_words": "John Tan",
        },
        {
            "target_id": "payee-ahma",
            "amount": "0.01",
            "amount_words": "the rest",
            "target_words": "Ah Ma",
        },
        {
            "target_id": "payee-mum",
            "amount": "0.01",
            "amount_words": "half of that",
            "target_words": "Mum",
        },
    ]
    bodies = [V.leg_body(turn, spec, i) for i, spec in enumerate(specs, 1)]
    bodies[2]["amount_expr"] = {"kind": "remaining_balance", "account_id": "acct-matthew-savings"}
    bodies[3]["amount_expr"] = {
        "kind": "fraction_of_leg",
        "leg_id": "leg-3",
        "numerator": 1,
        "denominator": 2,
    }
    response = freeze(v.env, conv, bodies)
    assert response.status_code == 200, response.text
    v.use_transport(ok_reply)
    v.validated(response.json()["bundle_id"])
    request = ValidationRequest.model_validate_json(v.sent[0].content)
    remaining, fraction = request.calculation_evidence
    assert [d.leg_id for d in remaining.preceding_debits] == ["leg-1", "leg-2"]
    assert remaining.result.to_str() == "860.00" and fraction.result.to_str() == "430.00"
    assert fraction.dependencies[0].leg_id == "leg-3"
    assert request_problem(request) is None
    data = request.model_dump(mode="json")
    data["calculation_evidence"][0]["preceding_debits"].reverse()
    assert request_problem(ValidationRequest.model_validate(data)) == "INVALID_REQUEST"
    parents = request.bundle.legs[2].lineage.parent_refs
    assert {p.kind for p in parents} == {"bank_record", "user_turn"}


def test_remaining_balance_after_equity_uses_actual_debit(v: V) -> None:
    v.sql("UPDATE accounts SET balance_cents = 100000 WHERE account_id = 'acct-matthew-savings'")
    token = v.env.issuer.issue_token("user-matthew", ["bank:read"])
    quote = v.client.post(
        "/internal/bank/quotes",
        json={"symbol": "D05"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    conv = new_conversation(v.env)
    turn = say(v.env, conv, "Buy 500 of DBS shares then send the rest to Mum from savings")
    equity = V.leg_body(
        turn,
        {
            "action": "buy_equity",
            "target_type": "sgx_symbol",
            "target_id": "D05",
            "amount": "500.00",
            "amount_words": "500",
            "target_words": "DBS",
            "equity_terms": {"quote_id": quote["quote_id"]},
        },
    )
    remaining = V.leg_body(
        turn,
        {
            "target_id": "payee-mum",
            "amount": "0.01",
            "amount_words": "the rest",
            "target_words": "Mum",
        },
        2,
    )
    remaining["amount_expr"] = {"kind": "remaining_balance", "account_id": "acct-matthew-savings"}
    response = freeze(v.env, conv, [equity, remaining])
    assert response.status_code == 200, response.text
    v.use_transport(ok_reply)
    v.validated(response.json()["bundle_id"])
    request = ValidationRequest.model_validate_json(v.sent[0].content)
    assert [leg.amount.to_str() for leg in request.bundle.legs] == ["462.00", "538.00"]
    item = request.calculation_evidence[0]
    assert item.preceding_debits[0].amount.to_str() == "462.00"
    assert item.result.to_str() == "538.00"
    assert request_problem(request) is None


def test_semantic_match_preserves_scam_step_up(v: V) -> None:
    _, bid = v.bundle(
        "A police officer told me to send 50 to John Tan from savings immediately",
        [
            {
                "target_id": "payee-johntan",
                "amount": "50.00",
                "amount_words": "50",
                "target_words": "John Tan",
            }
        ],
    )
    assert v.validated(bid)["verdict"] == "MATCH"
    issued = v.issue(bid)
    assert issued.status_code == 200, issued.text
    response = v.approve(issued.json())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "AWAITING_STEP_UP"
    assert "SCAM_SIGNAL_IMPERSONATION" in response.json()["reason_codes"]
    assert not v.q("SELECT * FROM receipts")
