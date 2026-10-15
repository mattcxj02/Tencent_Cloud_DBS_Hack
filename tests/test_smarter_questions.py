"""Smarter clarifications: choices for unknown names, voice hints, single-payment wording,
and asking about a payment a later message left out."""

from __future__ import annotations

from tests.agent_support import ACCOUNTS, PAYEES, FakeGateway, FakeLLM, intent, reply, run


def _body(gw: FakeGateway) -> dict:
    return gw.question_bodies[gw.questions[-1]["turn_id"]]


# ── Unknown names offer choices, with the account choices in the same card ──


def test_unknown_name_offers_every_saved_record_and_the_account_in_one_card() -> None:
    gw = FakeGateway()
    gw.say("Send fifteen dollars to Joy")
    out = run(gw, FakeLLM(reply(intent("transfer", "fifteen dollars", "Joy"))))
    assert out["reason"] == "RECIPIENT_NOT_FOUND"
    assert "Payment 1" not in out["question"]
    # The account question does not repeat the unknown name.
    assert out["question"].split("\n")[1].startswith("Which account should I use for this payment:")
    target, source = _body(gw)["fields"]
    assert target["field"] == "target" and source["field"] == "source"
    offered = {c["record_id"] for c in target["candidates"]}
    assert offered == {a["account_id"] for a in ACCOUNTS} | {p["payee_id"] for p in PAYEES}


def test_own_name_offers_own_accounts() -> None:
    gw = FakeGateway()
    gw.say("Send Matthew thirty dollars")
    out = run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Matthew"))))
    assert out["reason"] == "RECIPIENT_OWN_NAME"
    assert out["question"].startswith("“Matthew” is your own name.")
    target, _ = _body(gw)["fields"]
    assert {c["record_id"] for c in target["candidates"]} == {a["account_id"] for a in ACCOUNTS}


def test_a_longer_name_containing_the_owner_name_is_not_the_owner() -> None:
    gw = FakeGateway()
    gw.say("Send Matthew Lee thirty dollars")
    out = run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Matthew Lee"))))
    assert out["reason"] == "RECIPIENT_NOT_FOUND"


def test_spoken_unknown_name_points_to_transcript_correction() -> None:
    gw = FakeGateway()
    gw.speak("Send Joy fifteen dollars")
    out = run(gw, FakeLLM(reply(intent("transfer", "fifteen dollars", "Joy"))))
    assert (
        out["question"]
        .split("\n")[0]
        .endswith("If I misheard you, tap “Correct this transcript” above.")
    )

    typed = FakeGateway()
    typed.say("Send Joy fifteen dollars")
    out = run(typed, FakeLLM(reply(intent("transfer", "fifteen dollars", "Joy"))))
    assert "misheard" not in out["question"]


# ── Wording and account details ─────────────────────────────────────────


def test_one_payment_has_no_payment_prefix_but_two_do() -> None:
    gw = FakeGateway()
    gw.say("Send $50 to John")
    out = run(gw, FakeLLM(reply(intent("transfer", "$50", "John"))))
    assert out["question"].count("\n") == 1 and "Payment" not in out["question"]
    target, source = _body(gw)["fields"]
    assert target["field"] == "target" and "amount" not in target
    assert source["field"] == "source" and source["amount"] == "50.00"

    two = FakeGateway()
    two.say("Send $50 to John and $20 to Ah Ma")
    out = run(
        two,
        FakeLLM(reply(intent("transfer", "$50", "John"), intent("transfer", "$20", "Ah Ma"))),
    )
    assert out["question"].startswith("Payment 1: ")
    assert "\nPayment 2: " in out["question"]


def test_relative_amount_sends_no_advisory_amount() -> None:
    gw = FakeGateway()
    gw.say("Pay my card balance")
    run(gw, FakeLLM(reply(intent("pay_card", "card balance", "card"))))
    [source] = [f for f in _body(gw)["fields"] if f["field"] == "source"]
    assert "amount" not in source


# ── A payment the latest message left out ──────────────────────────────


def _matthew_then_john(gw: FakeGateway, second: str) -> dict:
    gw.speak("Send Matthew thirty dollars.")
    run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Matthew"))))
    gw.speak(second)
    return run(gw, FakeLLM(reply(intent("transfer", "fifty dollars", "John Tan", "savings"))))


def test_new_request_after_a_question_asks_about_the_left_out_payment() -> None:
    gw = FakeGateway()
    out = _matthew_then_john(gw, "Send John Tan fifty dollars from savings.")
    assert out["reason"] == "PAYMENT_LEFT_OUT"
    assert out["question"] == (
        "Your latest message didn't mention the payment of “thirty dollars” to “Matthew”. "
        "Should I still make it: Keep it or Leave it out?"
    )
    assert not gw.proposals

    gw.answer("Leave it out")
    done = run(gw, FakeLLM(reply(intent("transfer", "fifty dollars", "John Tan", "savings"))))
    assert done["status"] == "draft_frozen", done
    [leg] = gw.proposals[-1]["legs"]
    assert leg["target_id"] == "payee-johntan"


def test_keeping_the_left_out_payment_asks_about_it_again() -> None:
    gw = FakeGateway()
    _matthew_then_john(gw, "Send John Tan fifty dollars from savings.")
    gw.answer("Keep it")
    out = run(
        gw,
        FakeLLM(
            reply(
                intent("transfer", "thirty dollars", "Matthew"),
                intent("transfer", "fifty dollars", "John Tan", "savings"),
            )
        ),
    )
    assert out["reason"] == "RECIPIENT_OWN_NAME"


def test_keep_it_is_enforced_if_the_payment_is_still_missing() -> None:
    gw = FakeGateway()
    _matthew_then_john(gw, "Send John Tan fifty dollars from savings.")
    gw.answer("Keep it")
    out = run(gw, FakeLLM(reply(intent("transfer", "fifty dollars", "John Tan", "savings"))))
    assert out["reason"] == "PAYMENT_LEFT_OUT"
    assert not gw.proposals


def test_change_words_mean_the_payment_was_replaced_on_purpose() -> None:
    gw = FakeGateway()
    out = _matthew_then_john(gw, "Actually, send John Tan fifty dollars from savings.")
    assert out["status"] == "draft_frozen", out

    curly = FakeGateway()
    out = _matthew_then_john(curly, "I don’t want that, send John Tan fifty dollars from savings.")
    assert out["status"] == "draft_frozen", out


def test_choosing_which_four_payments_is_not_a_left_out_payment() -> None:
    gw = FakeGateway()
    gw.say("Send 1 each to Mum, Ah Ma, John Doe, John Tan and Landlord Lim from savings")
    five = [
        intent("transfer", "1", name, "savings")
        for name in ("Mum", "Ah Ma", "John Doe", "John Tan", "Landlord Lim")
    ]
    first = run(gw, FakeLLM(reply(*five)))
    assert first["reason"] == "TOO_MANY_LEGS"
    gw.answer("The first four")
    out = run(gw, FakeLLM(reply(*five[:4])))
    assert out["status"] == "draft_frozen", out


def test_same_amount_with_a_new_name_is_an_answer_not_a_new_payment() -> None:
    gw = FakeGateway()
    gw.say("Send thirty dollars to Joy from savings")
    run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Joy", "savings"))))
    gw.say("Ah Ma")
    out = run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Ah Ma", "savings"))))
    assert out["status"] == "draft_frozen", out


def test_corrected_transcript_is_not_a_left_out_payment() -> None:
    gw = FakeGateway()
    first = gw.speak("Send Matthew thirty dollars from savings.")
    run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Matthew", "savings"))))
    gw.edit(first["turn_id"], "Send Mum thirty dollars from savings.")
    out = run(gw, FakeLLM(reply(intent("transfer", "thirty dollars", "Mum", "savings"))))
    assert out["status"] == "draft_frozen", out
