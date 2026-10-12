"""Payee names: shared matching rules, aliases, customer nicknames and near spellings.

- ``shared.names``: exact matching ignores the spacing between whole words ("ahma" names
  "Ah Ma") but never matches part of a word; near spellings are suggestions only.
- Agent (fake gateway): aliases and nicknames resolve directly; a near spelling becomes a
  "Did you mean …?" question, and the answer resolves exactly.
- Gateway (real routes): payee search labels near spellings ``fuzzy``; the evidence check
  accepts aliases, nicknames and spacing variants but never a near spelling; the Payees
  routes add and remove nicknames with conflict checks, Origin checks and audit rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.questions import NONE_OF_THESE
from shared.names import compact, edit_distance, fuzzy_mentions, mentions
from tests.agent_support import FakeGateway, FakeLLM, intent, reply, run
from tests.orch_support import Orch, o_fixture  # noqa: F401
from tests.t05_support import H, freeze, leg, say
from tests.t10_support import v_fixture  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

AH_MA = "Ah Ma (account ending 7766)"


# ── shared.names ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["ahma", "Ah Ma", "ah-ma", "to AHMA please", "Ah.Ma"])
def test_spacing_and_punctuation_between_words_are_ignored(text: str) -> None:
    assert mentions(text, ["Ah Ma"])


@pytest.mark.parametrize(
    ("text", "name"),
    [("Mumbai", "Mum"), ("summer", "Mum"), ("ah mad", "Ah Ma"), ("johnny", "John")],
)
def test_exact_match_never_takes_part_of_a_word(text: str, name: str) -> None:
    assert not mentions(text, [name])


def test_joined_words_name_a_multi_word_name() -> None:
    assert mentions("send to johndoe", ["John Doe"])
    assert not mentions("send to johndoe", ["John"])  # a shared first name is not implied


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("mun", "Mum"),
        ("Ahmar", "Ah Ma"),
        ("Ah Mar", "Ah Mah"),
        ("Jhon", "John"),
        ("landlrod lim", "Landlord Lim"),
    ],
)
def test_near_spellings_are_suggested(text: str, name: str) -> None:
    assert not mentions(text, [name])
    assert fuzzy_mentions(text, [name])


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("to", "Tom"),  # filler words never match
        ("my", "Ma"),  # names under three letters are never fuzzy
        ("bum", "Mum"),  # the first letter must agree
        ("4521", "4522"),  # numbers are exact only
        ("priyanka", "Priya"),  # too far
    ],
)
def test_unrelated_words_are_not_suggested(text: str, name: str) -> None:
    assert not fuzzy_mentions(text, [name])


def test_edit_distance_counts_a_swap_as_one() -> None:
    assert edit_distance("jhon", "john") == 1
    assert edit_distance("mom", "mum") == 1
    assert compact(" Ah-Ma ") == "ahma"


# ── agent (fake gateway) ─────────────────────────────────────────────


@pytest.mark.parametrize(("words", "payee"), [("mom", "payee-mum"), ("ahma", "payee-ahma")])
def test_alias_and_spacing_variants_resolve_without_a_question(words: str, payee: str) -> None:
    gw = FakeGateway()
    gw.say(f"Send 20 to {words} from savings")
    out = run(gw, FakeLLM(reply(intent("transfer", "20", words, "savings"))))
    assert out["status"] == "draft_frozen", out
    [leg] = gw.proposals[-1]["legs"]
    assert leg["target_id"] == payee
    assert leg["evidence"]["target"]["text"] == words


def test_customer_nickname_resolves() -> None:
    gw = FakeGateway()
    gw.nicknames["Big Bro"] = "payee-johndoe"
    gw.say("Send 20 to Big Bro from savings")
    out = run(gw, FakeLLM(reply(intent("transfer", "20", "Big Bro", "savings"))))
    assert out["status"] == "draft_frozen", out
    assert gw.proposals[-1]["legs"][0]["target_id"] == "payee-johndoe"


def test_near_spelling_asks_did_you_mean_then_the_answer_resolves() -> None:
    gw = FakeGateway()
    gw.say("Send 20 to Ahmar from savings")
    first = run(gw, FakeLLM(reply(intent("transfer", "20", "Ahmar", "savings"))))

    assert first["status"] == "clarify"
    assert first["question"] == (
        f"I couldn't find “Ahmar” exactly. Did you mean: {AH_MA} or {NONE_OF_THESE}?"
    )
    assert not gw.proposals  # a near spelling is never proposed

    answer = gw.answer(AH_MA)
    out = run(gw, FakeLLM(reply(intent("transfer", "20", "Ah Ma", "savings"))))
    assert out["status"] == "draft_frozen", out
    [leg] = gw.proposals[-1]["legs"]
    assert leg["target_id"] == "payee-ahma"
    assert leg["evidence"]["target"]["turn_id"] == answer["turn_id"]


def test_near_spelling_of_a_shared_first_name_offers_every_match() -> None:
    gw = FakeGateway()
    gw.say("Send 20 to Jhon from savings")
    out = run(gw, FakeLLM(reply(intent("transfer", "20", "Jhon", "savings"))))
    assert out["status"] == "clarify"
    assert "John Doe (account ending 4521)" in out["question"]
    assert "John Tan (account ending 8892)" in out["question"]
    assert not gw.proposals


def test_no_near_spelling_keeps_the_not_found_question() -> None:
    gw = FakeGateway()
    gw.say("Send 20 to Zebedee from savings")
    out = run(gw, FakeLLM(reply(intent("transfer", "20", "Zebedee", "savings"))))
    assert out["status"] == "clarify"
    assert "Did you mean" not in out["question"]


# ── gateway: search and evidence (real routes) ───────────────────────


def _search(o: Orch, q: str) -> list[dict[str, Any]]:
    from gateway.bank_routes import get_bank_store

    return get_bank_store().search_payees("user-matthew", q)


def test_search_returns_names_and_labels_near_spellings(o: Orch) -> None:
    [mum] = _search(o, "mom")
    assert (mum["payee_id"], mum["match"]) == ("payee-mum", "exact")
    assert "Mom" in mum["names"]
    near = _search(o, "Ahmar")
    assert [(p["payee_id"], p["match"]) for p in near] == [("payee-ahma", "fuzzy")]


def _add(o: Orch, payee_id: str, nickname: str, headers: dict[str, str] | None = H):
    return o.client.post(
        f"/api/payees/{payee_id}/nicknames", json={"nickname": nickname}, headers=headers
    )


def test_gateway_accepts_a_nickname_and_a_spacing_variant_as_evidence(o: Orch) -> None:
    assert _add(o, "payee-johndoe", "Big Bro").status_code == 201
    for words, payee in (("Big Bro", "payee-johndoe"), ("ahma", "payee-ahma")):
        conv = o.conversation(f"Send $20 to {words} from my savings")
        o.extractor(reply(intent("transfer", "$20", words, "savings")))
        body = o.drafted(conv)
        assert body["status"] == "ready", body
        assert (
            body["legs"][0]["display_name"]
            == {
                "payee-johndoe": "John Doe",
                "payee-ahma": "Ah Ma",
            }[payee]
        )


def test_gateway_never_accepts_a_near_spelling_as_evidence(o: Orch) -> None:
    """A forged proposal citing "Ahmar" for Ah Ma is refused by the gateway itself."""
    conv = o.conversation()
    turn = say(o.env, conv, "Send $20 to Ahmar from my savings")
    body = leg(
        turn,
        amount="20.00",
        amount_words="$20",
        target_id="payee-ahma",
        target_words="Ahmar",
        source_words="savings",
    )
    resp = freeze(o.env, conv, [body])
    assert (resp.status_code, resp.json()["detail"]) == (400, "TARGET_EVIDENCE")


def test_agent_asks_instead_of_proposing_a_near_spelling(o: Orch) -> None:
    conv = o.conversation("Send $20 to Ahmar from my savings")
    o.extractor(reply(intent("transfer", "$20", "Ahmar", "savings")))
    body = o.drafted(conv)
    assert body["status"] == "clarify"
    assert body["question"]["text"].endswith(f"Did you mean: {AH_MA} or {NONE_OF_THESE}?")
    [(count,)] = o.v.q("SELECT COUNT(*) FROM frozen_drafts WHERE conversation_id = ?", (conv,))
    assert count == 0


# ── gateway: Payees routes ───────────────────────────────────────────


def test_payees_list_shows_aliases_once_and_nicknames(o: Orch) -> None:
    resp = o.client.get("/api/payees")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    mum = next(p for p in resp.json() if p["payee_id"] == "payee-mum")
    assert mum["aliases"] == ["Mama", "Mom", "Mommy", "Mummy"]  # "Mum"/"mum" = display name
    assert mum["nicknames"] == []
    assert "sanctioned" not in mum


def test_add_and_remove_a_nickname_is_audited(o: Orch) -> None:
    resp = _add(o, "payee-lim", "  Uncle   Lim ")
    assert resp.status_code == 201, resp.text
    assert resp.json()["nicknames"] == ["Uncle Lim"]

    resp = o.client.delete("/api/payees/payee-lim/nicknames/uncle lim", headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["nicknames"] == []
    steps = [r[0] for r in o.v.q("SELECT step FROM audit_events ORDER BY seq")]
    assert steps[-2:] == ["gateway.payee_nickname_added", "gateway.payee_nickname_removed"]


@pytest.mark.parametrize(
    ("payee_id", "nickname", "status", "code"),
    [
        ("payee-johndoe", "John", 409, "NICKNAME_CONFLICT"),  # names John Tan too
        ("payee-mum", "Doe", 409, "NICKNAME_CONFLICT"),  # a word of John Doe's name
        ("payee-mum", "Ah Ma", 409, "NICKNAME_CONFLICT"),  # another payee's name
        ("payee-mum", "ahma", 409, "NICKNAME_CONFLICT"),  # ... spelled without a space
        ("payee-mum", "Mum Lim", 409, "NICKNAME_CONFLICT"),  # contains "Lim"
        ("payee-mum", "DBS", 409, "NICKNAME_CONFLICT"),  # an SGX stock
        ("payee-mum", "Mom", 409, "NICKNAME_EXISTS"),  # already an alias
        ("payee-mum", "savings", 422, "NICKNAME_RESERVED"),
        ("payee-mum", "my", 422, "NICKNAME_RESERVED"),
        ("payee-mum", "<b>x</b>", 422, "NICKNAME_INVALID"),
        ("payee-mum", "Mum 2", 422, "NICKNAME_INVALID"),
        ("payee-ana-dad", "Pops", 404, "PAYEE_NOT_FOUND"),  # Ana's payee
    ],
)
def test_nickname_refusals(o: Orch, payee_id: str, nickname: str, status: int, code: str) -> None:
    resp = _add(o, payee_id, nickname)
    assert (resp.status_code, resp.json()["detail"]) == (status, code)


def test_a_nickname_cannot_be_reused_for_another_payee(o: Orch) -> None:
    assert _add(o, "payee-johndoe", "Big Bro").status_code == 201
    resp = _add(o, "payee-johntan", "bigbro")
    assert (resp.status_code, resp.json()["detail"]) == (409, "NICKNAME_CONFLICT")


def test_nickname_changes_need_the_origin_header(o: Orch) -> None:
    assert _add(o, "payee-mum", "Mother", headers={}).status_code == 403
    resp = o.client.delete("/api/payees/payee-mum/nicknames/mother")
    assert resp.status_code == 403


# ── upgrade an existing database ─────────────────────────────────────


def test_upgrade_adds_tables_and_aliases_and_keeps_existing_rows(tmp_path) -> None:
    from ledger.schema import connect
    from ledger.seed import seed_db, upgrade_db

    conn = connect(str(tmp_path / "old.db"))
    try:
        seed_db(conn)
        # An older database: no nickname table, only the original four Matthew aliases.
        conn.execute("DROP TABLE payee_nicknames")
        conn.execute("DELETE FROM payee_aliases WHERE alias NOT IN ('Mum','mum','Ah Ma','ah ma')")
        conn.execute("UPDATE accounts SET balance_cents = 1 WHERE account_id = 'acct-joint-01'")
        conn.commit()

        added = upgrade_db(conn)
        assert added == 9  # seven new Matthew aliases, plus Ana's two
        assert upgrade_db(conn) == 0  # idempotent
        assert conn.execute("SELECT COUNT(*) FROM payee_nicknames").fetchone()[0] == 0
        row = conn.execute("SELECT balance_cents FROM accounts WHERE account_id = 'acct-joint-01'")
        assert row.fetchone()[0] == 1  # existing data untouched
    finally:
        conn.close()
