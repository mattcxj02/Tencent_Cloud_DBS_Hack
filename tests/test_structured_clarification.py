"""Grouped questions and verified answers through real gateway/agent/validator routes."""

from __future__ import annotations

import copy

import pytest

from gateway.audit import verify_chain
from ledger.schema import connect
from tests.agent_support import intent, reply
from tests.orch_support import AGENT_SCOPES, Orch, o_fixture  # noqa: F401
from tests.t05_support import H, cite, freeze, leg, say
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import ScriptedLLM

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def grouped(o: Orch, text: str = "Send $50") -> tuple[str, dict]:
    conv = o.conversation(text)
    o.extractor(reply(intent("transfer", "$50")))
    result = o.drafted(conv)
    assert result["status"] == "clarify", result
    return conv, result["question"]


def answer_body(question: dict, labels: tuple[str, ...] = ("Mum", "savings")) -> dict:
    fields = question["clarification"]["fields"]
    selections = []
    for f, label in zip(fields, labels, strict=True):
        [choice] = [c for c in f["choices"] if label.casefold() in c["label"].casefold()]
        selections.append(
            {
                "field_id": f["field_id"],
                "leg_id": f["leg_id"],
                "field": f["field"],
                "choice_id": choice["choice_id"],
            }
        )
    return {
        "answers_question_id": question["turn_id"],
        "question_revision": question["clarification"]["revision"],
        "selections": selections,
    }


def answer(o: Orch, conv: str, body: dict, headers: dict | None = None):
    return o.client.post(f"/api/conversations/{conv}/answer", json=body, headers=headers or H)


def test_grouped_recipient_and_source_resume_without_extraction_and_validate(o: Orch) -> None:
    conv, question = grouped(o)
    fields = question["clarification"]["fields"]
    assert [(f["leg_id"], f["field"]) for f in fields] == [("leg-1", "target"), ("leg-1", "source")]
    assert all("record_id" not in c for f in fields for c in f["choices"])
    assert len(o.llm.calls) == 1 and not o.v.llm.calls
    response = answer(o, conv, answer_body(question))
    assert response.status_code == 200, response.text
    turn = response.json()
    # Reads as the customer's answer, with no internal leg or field names.
    assert turn["text"] == "Mum (account ending 9988), from savings (account ending 1234)"
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    assert result["conversation_revision"] == 2
    assert result["legs"][0]["display_name"] == "Mum"
    assert result["legs"][0]["source_account_id"] == "acct-matthew-savings"
    assert len(o.llm.calls) == 1  # No second extractor generation.
    assert len(o.v.llm.calls) == 1  # Final independent semantic validation still runs.
    bundle = o.env.drafts.get_draft(result["bundle_id"], "user-matthew")
    assert bundle is not None
    assert any(
        r.kind == "user_selection" and r.turn_id == turn["turn_id"]
        for r in bundle.legs[0].lineage.source_refs
    )
    conn = connect(o.env.db_path)
    try:
        assert verify_chain(conn, conversation_id=conv)["ok"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("choice_id", "CHOICE_ID_INVALID"),
        ("field_id", "CHOICE_BINDING_INVALID"),
        ("leg_id", "CHOICE_BINDING_INVALID"),
        ("field", "CHOICE_BINDING_INVALID"),
        ("revision", "STALE_QUESTION_REVISION"),
        ("duplicate", "CHOICE_BINDING_INVALID"),
        ("partial", "CHOICE_ANSWER_AMBIGUOUS"),
        ("question", "CHOICE_QUESTION_INVALID"),
    ],
)
def test_reject_forged_stale_wrong_leg_and_ambiguous_answers(
    o: Orch, mutation: str, code: str
) -> None:
    conv, question = grouped(o)
    body = answer_body(question)
    if mutation in ("choice_id", "field_id"):
        body["selections"][0][mutation] = "forged"
    elif mutation == "leg_id":
        body["selections"][0]["leg_id"] = "leg-2"
    elif mutation == "field":
        body["selections"][0]["field"] = "source"
    elif mutation == "revision":
        body["question_revision"] -= 1
    elif mutation == "duplicate":
        body["selections"][1] = copy.deepcopy(body["selections"][0])
    elif mutation == "partial":
        body["selections"].pop()
    else:
        body["answers_question_id"] = "turn-forged"
    response = answer(o, conv, body)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == code
    stored = o.client.get(f"/api/conversations/{conv}").json()
    assert stored["revision"] == 1
    assert not any(t["role"] == "user_answer" for t in stored["turns"])
    assert len(o.llm.calls) == 1 and not o.v.llm.calls


def test_origin_session_and_owner_are_required(o: Orch) -> None:
    conv, question = grouped(o)
    body = answer_body(question)
    assert answer(o, conv, body, {"Origin": "https://attacker.invalid"}).status_code == 403
    o.env.as_user("user-alice")
    assert answer(o, conv, body).status_code == 404


@pytest.mark.parametrize("change", ["text", "edit", "new_question", "answered"])
def test_question_is_invalidated_by_input_new_question_and_replay(o: Orch, change: str) -> None:
    conv, question = grouped(o)
    body = answer_body(question)
    if change == "text":
        say(o.env, conv, "Actually send fifteen to Ah Ma from current")
    elif change == "edit":
        turn = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
        r = o.client.post(
            f"/api/conversations/{conv}/transcript-edit",
            json={
                "edits_turn_id": turn["turn_id"],
                "text": "Send $15 to Ah Ma from current",
            },
        )
        assert r.status_code == 200
    elif change == "new_question":
        o.drafted(conv)
    else:
        assert answer(o, conv, body).status_code == 200
    r = answer(o, conv, body)
    assert r.status_code == 400 and r.json()["detail"] == "STALE_QUESTION"


@pytest.mark.parametrize(
    "record_type,record_id",
    [
        ("own_account", "acct-ana-savings"),
        ("saved_payee", "payee-ana-dad"),
        ("saved_payee", "payee-forged"),
    ],
)
def test_agent_cannot_offer_foreign_or_forged_records(
    o: Orch, record_type: str, record_id: str
) -> None:
    conv = o.conversation("Send $50")
    token = o.env.issuer.issue_token("user-matthew", AGENT_SCOPES, conversation_id=conv)
    r = o.client.post(
        f"/internal/conversations/{conv}/questions",
        headers={
            "Authorization": f"Bearer {token}",
        },
        json={
            "text": "Who?",
            "revision": 1,
            "fields": [
                {
                    "leg_id": "leg-1",
                    "field": "target",
                    "prompt": "Who?",
                    "candidates": [{"target_type": record_type, "record_id": record_id}],
                }
            ],
        },
    )
    assert r.status_code == 404 and r.json()["detail"] == "CHOICE_RECORD_INVALID"
    assert len(o.client.get(f"/api/conversations/{conv}").json()["turns"]) == 1


def test_choices_from_another_conversation_are_invalid(o: Orch) -> None:
    conv, q = grouped(o)
    _, other = grouped(o)
    body = answer_body(q)
    body["selections"][0]["choice_id"] = other["clarification"]["fields"][0]["choices"][0][
        "choice_id"
    ]
    assert answer(o, conv, body).json()["detail"] == "CHOICE_ID_INVALID"


def test_ambiguous_display_labels_are_not_accepted_as_a_selection(o: Orch) -> None:
    o.env.sql(
        "UPDATE payees SET display_name = 'John Doe', masked_suffix = '4521' "
        "WHERE payee_id = 'payee-johntan'"
    )
    conv = o.conversation("Send $50 to John from savings")
    o.extractor(reply(intent("transfer", "$50", "John", "savings")))
    q = o.drafted(conv)["question"]
    [f] = q["clarification"]["fields"]
    assert len(f["choices"]) == 2 and f["choices"][0]["label"] == f["choices"][1]["label"]
    body = {
        "answers_question_id": q["turn_id"],
        "question_revision": 1,
        "selections": [
            {
                "field_id": f["field_id"],
                "leg_id": f["leg_id"],
                "field": f["field"],
                "choice_id": f["choices"][0]["choice_id"],
            }
        ],
    }
    assert answer(o, conv, body).json()["detail"] == "CHOICE_ANSWER_AMBIGUOUS"


def test_offered_record_must_still_be_owned_when_answered(o: Orch) -> None:
    conv, q = grouped(o)
    body = answer_body(q)
    o.env.sql("UPDATE payees SET owner_user_id = 'user-ana' WHERE payee_id = 'payee-mum'")
    assert answer(o, conv, body).json()["detail"] == "CHOICE_RECORD_INVALID"


def test_labels_changed_after_selection_cannot_supply_frozen_evidence(o: Orch) -> None:
    conv, q = grouped(o)
    response = answer(o, conv, answer_body(q))
    assert response.status_code == 200
    turn = response.json()
    original = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
    body = leg(
        turn,
        amount="50.00",
        amount_words="9988",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    body["evidence"] = {
        "amount": cite(original, "$50"),
        "target": cite(turn, "Mum (account ending 9988)"),
        "source": cite(turn, "savings (account ending 1234)"),
    }
    o.env.sql("UPDATE payees SET display_name = 'Changed Mum' WHERE payee_id = 'payee-mum'")
    r = freeze(o.env, conv, [body])
    assert r.status_code == 400 and r.json()["detail"] == "SELECTION_RECORD_CHANGED"


def test_free_text_cannot_be_silently_discarded_alongside_selections(o: Orch) -> None:
    conv, q = grouped(o)
    body = answer_body(q)
    body["text"] = "Actually fifteen to Ah Ma from current"
    r = answer(o, conv, body)
    assert r.status_code == 400 and r.json()["detail"] == "CHOICE_WITH_FREE_TEXT"
    assert o.client.get(f"/api/conversations/{conv}").json()["revision"] == 1


def test_client_cannot_supply_record_ids_or_labels_as_selection_evidence(o: Orch) -> None:
    conv, q = grouped(o)
    body = answer_body(q)
    body["selections"][0]["record_id"] = "payee-ana-dad"
    body["selections"][0]["label"] = "Mum"
    assert answer(o, conv, body).status_code == 422


def test_same_record_choice_cannot_be_reused_for_another_leg(o: Orch) -> None:
    conv = o.conversation("Send $50 to Mum and $20 to Ah Ma")
    o.extractor(reply(intent("transfer", "$50", "Mum"), intent("transfer", "$20", "Ah Ma")))
    q = o.drafted(conv)["question"]
    body = answer_body(q, ("savings", "savings"))
    body["selections"][1]["choice_id"] = body["selections"][0]["choice_id"]
    assert answer(o, conv, body).json()["detail"] == "CHOICE_ID_INVALID"


def test_bank_choices_are_scoped_to_the_authenticated_owner(o: Orch) -> None:
    conv = o.conversation("Send $50")
    token = o.env.issuer.issue_token("user-matthew", AGENT_SCOPES, conversation_id=conv)
    r = o.client.get("/internal/bank/payees", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    ids = {p["payee_id"] for p in r.json()}
    assert "payee-mum" in ids and "payee-ana-dad" not in ids


@pytest.mark.parametrize("words", ["Ahmar", "Jhon"])
def test_fuzzy_suggestions_need_authenticated_selection_and_skip_second_extraction(
    o: Orch, words: str
) -> None:
    conv = o.conversation(f"Send $50 to {words} from savings")
    o.extractor(reply(intent("transfer", "$50", words, "savings")))
    q = o.drafted(conv)["question"]
    label = "Ah Ma" if words == "Ahmar" else "John Doe"
    assert answer(o, conv, answer_body(q, (label,))).status_code == 200
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    assert result["legs"][0]["display_name"] == label
    assert len(o.llm.calls) == 1 and len(o.v.llm.calls) == 1


def test_group_with_missing_amount_collects_other_fields_and_uses_free_text(o: Orch) -> None:
    conv = o.conversation("Send money")
    o.extractor(reply(intent("transfer")))
    q = o.drafted(conv)["question"]
    assert "How much" in q["text"] and "Who" in q["text"] and "account" in q["text"]
    assert q["clarification"]["fields"] == []  # Amount words require extraction.
    assert (
        answer(
            o,
            conv,
            {"answers_question_id": q["turn_id"], "text": "Send fifteen to Mum from savings"},
        ).status_code
        == 200
    )
    o.llm._replies = [reply(intent("transfer", "fifteen", "Mum", "savings"))]
    assert o.drafted(conv)["status"] == "ready"
    assert len(o.llm.calls) == 2


def test_free_text_answer_changes_amount_source_and_recipient(o: Orch) -> None:
    conv = o.conversation("Send $50 to John")
    o.extractor(reply(intent("transfer", "$50", "John")))
    q = o.drafted(conv)["question"]
    r = answer(
        o,
        conv,
        {
            "answers_question_id": q["turn_id"],
            "text": "Actually send fifteen to Ah Ma from current",
        },
    )
    assert r.status_code == 200
    o.llm._replies = [reply(intent("transfer", "fifteen", "Ah Ma", "current"))]
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    assert len(o.llm.calls) == 2
    [result_leg] = result["legs"]
    assert (result_leg["amount"], result_leg["display_name"], result_leg["source_account_id"]) == (
        "15.00",
        "Ah Ma",
        "acct-matthew-current",
    )


def test_correction_after_structured_answer_does_not_reuse_checkpoint(o: Orch) -> None:
    conv, q = grouped(o)
    assert answer(o, conv, answer_body(q)).status_code == 200
    original = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
    r = o.client.post(
        f"/api/conversations/{conv}/transcript-edit",
        json={
            "edits_turn_id": original["turn_id"],
            "text": "Send $15 to Ah Ma from current",
        },
    )
    assert r.status_code == 200
    o.llm._replies = [reply(intent("transfer", "$15", "Ah Ma", "current"))]
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    assert len(o.llm.calls) == 2
    assert result["legs"][0]["amount"] == "15.00"
    assert result["legs"][0]["display_name"] == "Ah Ma"


def test_relative_and_remaining_amounts_recomputed_from_structured_sources(o: Orch) -> None:
    conv = o.conversation("Send $100 to Mum, half of that to Ah Ma, then the rest to John Doe")
    o.extractor(
        reply(
            intent("transfer", "$100", "Mum"),
            intent("transfer", "half of that", "Ah Ma"),
            intent("transfer", "the rest", "John Doe"),
        )
    )
    q = o.drafted(conv)["question"]
    assert [f["leg_id"] for f in q["clarification"]["fields"]] == ["leg-1", "leg-2", "leg-3"]
    o.env.sql(
        "UPDATE accounts SET balance_cents = 1100000 WHERE account_id = 'acct-matthew-savings'"
    )
    response = answer(o, conv, answer_body(q, ("savings", "savings", "savings")))
    assert response.status_code == 200
    assert response.json()["text"] == "; ".join(
        f"Payment {n}: from savings (account ending 1234)" for n in (1, 2, 3)
    )
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    # Gateway recomputes ordered debits against its current authoritative balance.
    bundle = o.env.drafts.get_draft(result["bundle_id"], "user-matthew")
    assert bundle is not None
    assert [item.amount.to_str() for item in bundle.legs] == ["100.00", "50.00", "10850.00"]
    assert len(o.llm.calls) == 1 and len(o.v.llm.calls) == 1


def test_uncertain_validation_after_structured_answer_still_blocks_confirmation(o: Orch) -> None:
    conv, q = grouped(o)
    assert answer(o, conv, answer_body(q)).status_code == 200
    o.v.use_llm(ScriptedLLM.verdict("UNCERTAIN"))
    result = o.drafted(conv)
    assert result["status"] == "needs_correction"
    assert not result["validation"]["eligible_for_confirmation"]
    assert len(o.llm.calls) == 1 and len(o.v.llm.calls) == 1


@pytest.mark.parametrize("attack", ["leg", "field", "record", "revision", "amount"])
def test_gateway_independently_checks_selection_provenance_at_freeze(o: Orch, attack: str) -> None:
    conv, q = grouped(o)
    response = answer(o, conv, answer_body(q))
    turn = response.json()
    original = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
    body = leg(
        turn,
        amount="50.00",
        amount_words="9988",
        target_id="payee-mum",
        target_words="Mum",
        source_words="savings",
    )
    body["evidence"] = {
        "amount": cite(original, "$50"),
        "target": cite(turn, "Mum (account ending 9988)"),
        "source": cite(turn, "savings (account ending 1234)"),
    }
    if attack == "leg":
        body["leg_id"] = "leg-2"
    elif attack == "field":
        body["evidence"]["target"], body["evidence"]["source"] = (
            body["evidence"]["source"],
            body["evidence"]["target"],
        )
    elif attack == "record":
        body["target_id"] = "payee-ahma"
    elif attack == "revision":
        say(o.env, conv, "Keep that request")
    else:
        body["evidence"]["amount"] = cite(turn, "9988")
        body["amount_expr"] = {"kind": "literal", "amount": "9988.00"}
    r = freeze(o.env, conv, [body])
    assert r.status_code == 400, r.text
    assert r.json()["detail"] in ("SELECTION_BINDING_INVALID", "SELECTION_REVISION_INVALID")
    assert not o.v.q("SELECT bundle_id FROM frozen_drafts WHERE conversation_id = ?", (conv,))


def test_account_choices_show_live_balances_and_mark_short_accounts(o: Orch) -> None:
    conn = connect(o.env.db_path)
    try:
        balances = {
            r["type"]: r["balance_cents"]
            for r in conn.execute(
                "SELECT a.type, a.balance_cents FROM accounts a JOIN account_holders h "
                "ON h.account_id = a.account_id WHERE h.user_id = 'user-matthew'"
            )
        }
    finally:
        conn.close()
    # An amount between the smallest and largest balance, so some accounts are short.
    cents = sorted(balances.values())
    amount = (cents[0] + cents[-1]) // 200
    conv = o.conversation(f"Send ${amount} to Mum")
    o.extractor(reply(intent("transfer", f"${amount}", "Mum")))
    question = o.drafted(conv)["question"]
    [source] = [f for f in question["clarification"]["fields"] if f["field"] == "source"]
    assert source["amount"] == f"{amount}.00"
    for choice in source["choices"]:
        kind = choice["label"].split(" ")[0]
        assert choice["available"] == f"{balances[kind] / 100:.2f}"
        assert choice.get("insufficient", False) == (balances[kind] < amount * 100)
    assert any(c.get("insufficient") for c in source["choices"])
    assert not all(c.get("insufficient") for c in source["choices"])
    # The live balance is display only: the frozen label is unchanged and still answers.
    assert all("available" not in c["label"] for c in source["choices"])


def test_snapshot_carries_owner_name_and_last_question_checkpoint(o: Orch) -> None:
    from gateway.conv_routes import get_conversation_store

    conv, question = grouped(o)
    snapshot = get_conversation_store().get_snapshot(conv)
    assert snapshot["owner_name"] == "Matthew"
    assert snapshot["last_question"]["turn_id"] == question["turn_id"]
    assert snapshot["last_question"]["extraction"]["intents"][0]["amount_quote"] == "$50"


def test_unknown_name_choices_resolve_recipient_and_account_in_one_answer(o: Orch) -> None:
    conv = o.conversation("Send $15 to Joy")
    o.extractor(reply(intent("transfer", "$15", "Joy")))
    question = o.drafted(conv)["question"]
    fields = question["clarification"]["fields"]
    assert [f["field"] for f in fields] == ["target", "source"]
    response = answer(o, conv, answer_body(question, ("Mum", "savings")))
    assert response.status_code == 200, response.text
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    assert result["legs"][0]["display_name"] == "Mum"


def test_own_name_choices_become_an_own_account_transfer(o: Orch) -> None:
    conv = o.conversation("Send Matthew $15")
    o.extractor(reply(intent("transfer", "$15", "Matthew")))
    question = o.drafted(conv)["question"]
    assert question["text"].startswith("“Matthew” is your own name.")
    response = answer(o, conv, answer_body(question, ("savings", "current")))
    assert response.status_code == 200, response.text
    result = o.drafted(conv)
    assert result["status"] == "ready", result
    bundle = o.env.drafts.get_draft(result["bundle_id"], "user-matthew")
    assert bundle is not None
    [leg] = bundle.legs
    assert (leg.target_id, leg.source_account_id) == (
        "acct-matthew-savings",
        "acct-matthew-current",
    )
