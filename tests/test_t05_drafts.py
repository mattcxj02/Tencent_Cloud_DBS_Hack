"""T-05 frozen drafts: untrusted agent proposals become gateway-resolved bundles.

The gateway resolves display data and arithmetic itself, checks ownership, and
builds lineage only from verified citations of user words (invariant I7).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.t05_support import (
    Env,
    cite,
    freeze,
    leg,
    new_conversation,
    say,
    simple_bundle,
)


def _bundle(e: Env, bundle_id: str) -> dict:
    with sqlite3.connect(e.db_path) as conn:
        row = conn.execute(
            "SELECT canonical_bytes, status FROM frozen_drafts WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
    return {"bundle": json.loads(row[0]), "status": row[1]}


def test_freeze_resolves_display_from_bank_records(env: Env) -> None:
    _, bundle_id = simple_bundle(env)
    stored = _bundle(env, bundle_id)
    (frozen_leg,) = stored["bundle"]["legs"]
    assert stored["status"] == "DRAFT_FROZEN"
    assert frozen_leg["amount"] == "50.00"
    assert frozen_leg["display_metadata"] == {
        "display_name": "Mum",
        "masked_account_suffix": "9988",
    }
    kinds = [ref["kind"] for ref in frozen_leg["lineage"]["source_refs"]]
    assert kinds.count("user_turn") == 3 and kinds.count("bank_record") == 2


def test_agent_cannot_supply_display_names(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
        display_name="Totally Legit Mum",
    )
    assert freeze(env, conv, [body]).status_code == 422


def test_ambiguous_first_name_rejected_then_resolved(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to John from my savings")
    ambiguous = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-johndoe",
        target_words="John",
        source_words="savings",
    )
    resp = freeze(env, conv, [ambiguous])
    assert resp.status_code == 400
    assert resp.json()["detail"] == "TARGET_EVIDENCE_AMBIGUOUS"

    answer = say(env, conv, "John Doe, the one ending 4521")
    resolved = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-johndoe",
        target_words="John",
        source_words="savings",
    )
    resolved["evidence"]["target"] = cite(answer, "4521")
    assert freeze(env, conv, [resolved]).status_code == 200


def test_words_must_name_the_proposed_record(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    swapped = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-lim",  # agent swapped in a different payee
        target_words="Mum",
        source_words="savings",
    )
    resp = freeze(env, conv, [swapped])
    assert resp.status_code == 400
    assert resp.json()["detail"] == "TARGET_EVIDENCE"


def test_other_users_records_rejected(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    for overrides in ({"source_id": "acct-ana-savings"}, {"target_id": "payee-ana-dad"}):
        body = leg(
            turn,
            amount="50.00",
            amount_words="50",
            target_id=overrides.get("target_id", "payee-mum"),
            target_words="Mum",
            source_words="savings",
            source_id=overrides.get("source_id", "acct-matthew-savings"),
        )
        assert freeze(env, conv, [body]).status_code == 400


def test_evidence_required_and_location_checked(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    missing = {**body, "evidence": None}
    assert freeze(env, conv, [missing]).json()["detail"] == "EVIDENCE_MISSING"

    shifted = json.loads(json.dumps(body))
    shifted["evidence"]["amount"]["start"] += 1
    assert freeze(env, conv, [shifted]).json()["detail"] == "EVIDENCE_SPAN_MISMATCH"


def test_cannot_cite_app_question_or_edited_turn(env: Env) -> None:
    conv = new_conversation(env)
    original = say(env, conv, "Send 50 to Mum from my savings")
    token = env.issuer.issue_token(
        "user-matthew", ["conversation:propose_question"], conversation_id=conv
    )
    question = env.client.post(
        f"/internal/conversations/{conv}/questions",
        json={"text": "Send 900 to Mum from savings?"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    via_question = leg(
        original,
        amount="900.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    via_question["evidence"]["amount"] = cite(question, "900")
    assert freeze(env, conv, [via_question]).json()["detail"] == "EVIDENCE_TURN_INVALID"

    env.client.post(
        f"/api/conversations/{conv}/transcript-edit",
        json={"edits_turn_id": original["turn_id"], "text": "Send 15 to Mum from my savings"},
    )
    stale = leg(
        original,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    assert freeze(env, conv, [stale]).json()["detail"] == "EVIDENCE_TURN_INVALID"


def test_bill_reference_cannot_supply_amount(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Pay SP Group 50 from savings, bill reference INV 900")
    body = leg(
        turn,
        amount="900.00",
        amount_words="900",
        target_id="biller-spgroup",
        target_words="SP Group",
        source_words="savings",
        action="pay_bill",
        target_type="biller",
        reference="INV 900",
    )
    assert freeze(env, conv, [body]).json()["detail"] == "AMOUNT_FROM_REFERENCE"


def test_stale_revision_and_foreign_token_rejected(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    say(env, conv, "and thanks")  # the conversation moves on before the agent proposes
    resp = freeze(env, conv, [body], rev=1)
    assert resp.json()["detail"] == "STALE_CONVERSATION_REVISION"

    other = new_conversation(env)
    token = env.issuer.issue_token("user-matthew", ["draft:freeze"], conversation_id=other)
    resp = env.client.post(
        "/internal/drafts",
        json={"conversation_id": conv, "conversation_revision": 1, "legs": [body]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_relative_amount_is_derived_with_inherited_lineage(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 100 to Mum from my savings and half of that to Ah Ma from savings")
    first = leg(
        turn,
        amount="100.00",
        amount_words="100",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    second = leg(
        turn,
        amount="0.01",
        amount_words="half of that",
        target_id="payee-ahma",
        target_words="Ah Ma",
        source_words="savings",
        leg_id="leg-2",
    )
    second["evidence"]["source"] = cite(turn, "savings", occurrence=1)
    second["amount_expr"] = {
        "kind": "fraction_of_leg",
        "leg_id": "leg-1",
        "numerator": 1,
        "denominator": 2,
    }
    resp = freeze(env, conv, [first, second])
    assert resp.status_code == 200, resp.text
    legs = _bundle(env, resp.json()["bundle_id"])["bundle"]["legs"]
    assert [x["amount"] for x in legs] == ["100.00", "50.00"]
    assert legs[1]["lineage"]["source_refs"][0]["kind"] == "derived"
    assert {r["kind"] for r in legs[1]["lineage"]["parent_refs"]} == {"user_turn"}


def test_equity_terms_computed_by_gateway(env: Env) -> None:
    token = env.issuer.issue_token("user-matthew", ["bank:read"])
    quote = env.client.post(
        "/internal/bank/quotes",
        json={"symbol": "D05"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    conv = new_conversation(env)
    turn = say(env, conv, "Buy 500 dollars of DBS shares from my savings")
    body = leg(
        turn,
        amount="500.00",
        amount_words="500 dollars",
        target_id="D05",
        target_words="DBS",
        source_words="savings",
        action="buy_equity",
        target_type="sgx_symbol",
        equity_terms={"quote_id": quote["quote_id"], "quantity": 999},
    )
    resp = freeze(env, conv, [body])
    assert resp.status_code == 200, resp.text
    (frozen_leg,) = _bundle(env, resp.json()["bundle_id"])["bundle"]["legs"]
    terms = frozen_leg["equity_terms"]
    assert terms["quantity"] == 12  # floor(500 / 38.50); the agent's 999 is ignored
    assert terms["actual_debit"] == "462.00" == frozen_leg["amount"]
    assert terms["residual_cash"] == "38.00"

    wrong_symbol = {**body, "target_id": "O39", "evidence": {**body["evidence"]}}
    assert freeze(env, conv, [wrong_symbol]).status_code == 400


def test_new_user_turn_supersedes_frozen_draft(env: Env) -> None:
    conv, bundle_id = simple_bundle(env)
    say(env, conv, "Actually make it 15")
    assert _bundle(env, bundle_id)["status"] == "SUPERSEDED"


def test_full_name_beats_shared_first_name(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to John Doe from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-johndoe",
        target_words="John Doe",
        source_words="savings",
    )
    assert freeze(env, conv, [body]).status_code == 200


def test_amount_words_must_mean_the_proposed_amount(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send fifteen dollars to Mum from my savings")
    inflated = leg(
        turn,
        amount="50.00",  # the model "heard" fifty
        amount_words="fifteen dollars",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    assert freeze(env, conv, [inflated]).json()["detail"] == "AMOUNT_EVIDENCE_MISMATCH"
    correct = {**inflated, "amount_expr": {"kind": "literal", "amount": "15.00"}}
    assert freeze(env, conv, [correct]).status_code == 200


def test_card_balance_words_must_match_outstanding(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Pay my card balance from my current account")
    body = leg(
        turn,
        amount="642.30",
        amount_words="card balance",
        target_id="card-matthew-01",
        target_words="card",
        source_words="current",
        source_id="acct-matthew-current",
        action="pay_card",
        target_type="owned_card",
    )
    assert freeze(env, conv, [body]).status_code == 200
    wrong = {**body, "amount_expr": {"kind": "literal", "amount": "999.00"}}
    assert freeze(env, conv, [wrong]).json()["detail"] == "AMOUNT_EVIDENCE_MISMATCH"


@pytest.mark.parametrize(
    "amount_expr",
    [
        {"kind": "literal", "amount": "50.001"},
        {"kind": "fraction_of_leg", "leg_id": "leg-1", "numerator": 3, "denominator": 2},
        {"kind": "fraction_of_leg", "leg_id": "leg-9", "numerator": 1, "denominator": 2},
        {"kind": "mystery"},
    ],
    ids=["too-precise", "fraction-over-one", "unknown-leg", "unknown-kind"],
)
def test_malformed_amount_expression_is_a_400_with_code(env: Env, amount_expr: dict) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    body["amount_expr"] = amount_expr
    resp = freeze(env, conv, [body])
    assert (resp.status_code, resp.json()["detail"]) == (400, "AMOUNT_EXPR_INVALID")


def test_freeze_response_includes_bundle_hash(env: Env) -> None:
    conv = new_conversation(env)
    turn = say(env, conv, "Send 50 to Mum from my savings")
    body = leg(
        turn,
        amount="50.00",
        amount_words="50",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    resp = freeze(env, conv, [body]).json()
    stored = _bundle(env, resp["bundle_id"])["bundle"]
    import hashlib

    from shared.canonical import canonical_bytes

    assert resp["bundle_hash"] == hashlib.sha256(canonical_bytes(stored)).hexdigest()
