"""T-09 agent building blocks: turns, quote location, name matching, extractor I/O,
prompt hash, graph shape and the HTTP endpoint."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import app as agent_app
from agent.extraction import (
    OpenAICompatibleExtractor,
    build_extraction_input,
    parse_extractor_output,
)
from agent.gateway_client import GatewayClient
from agent.names import (
    account_record,
    match_records,
    narrow_citation,
    payee_queries,
    payee_record,
)
from agent.prompt import EXTRACTOR_PROMPT_PATH, load_extractor_prompt
from agent.turns import LocateError, Span, find_spans, locate, quoted_regions, visible_turns
from agent.workflow import AgentDeps, build_workflow
from tests.agent_support import ACCOUNTS, PAYEES, TOKEN, FakeGateway, FakeLLM, intent, reply

ROOT = Path(__file__).resolve().parent.parent


def _turn(seq: int, role: str, text: str, edits: str | None = None) -> dict[str, Any]:
    return {"turn_id": f"t{seq}", "seq": seq, "role": role, "text": text, "edits_turn_id": edits}


# ── Turns ─────────────────────────────────────────────────────────────


def test_visible_turns_apply_corrections_in_place() -> None:
    raw = [
        _turn(1, "user_text", "send fifty to Mum"),
        _turn(2, "app_question", "Which account?"),
        _turn(3, "user_answer", "savings"),
        _turn(4, "user_transcript_edit", "send fifteen to Mum", edits="t1"),
        _turn(5, "user_transcript_edit", "send sixteen to Mum", edits="t4"),
        _turn(6, "user_asr_final", "thanks"),
        _turn(7, "system_note", "ignored"),
    ]
    turns = visible_turns(reversed(raw))
    assert [(t.turn_id, t.text) for t in turns] == [
        ("t5", "send sixteen to Mum"),  # latest correction, at the original position
        ("t2", "Which account?"),
        ("t3", "savings"),
        ("t6", "thanks"),
    ]
    assert [t.is_user for t in turns] == [True, False, True, True]


def test_visible_turns_two_edits_of_one_turn_keep_the_latest() -> None:
    raw = [
        _turn(1, "user_text", "fifty"),
        _turn(2, "user_transcript_edit", "fifteen", edits="t1"),
        _turn(3, "user_transcript_edit", "sixteen", edits="t1"),
    ]
    assert [t.text for t in visible_turns(raw)] == ["sixteen"]


def test_find_spans_whole_words_case_insensitive_verbatim_text() -> None:
    turns = visible_turns([_turn(1, "user_text", "Send 20 to mum and Mama; MUM again")])
    spans = find_spans("Mum", turns)
    assert [s.text for s in spans] == ["mum", "MUM"]  # the user's own casing is cited
    assert all(turns[0].text[s.start : s.end] == s.text for s in spans)
    assert find_spans("Ma", turns) == []
    assert find_spans("  ", turns) == []


def test_find_spans_never_in_app_questions() -> None:
    turns = visible_turns(
        [
            _turn(1, "user_text", "send 50 to John"),
            _turn(2, "app_question", "John Doe or John Tan?"),
        ]
    )
    assert find_spans("John Doe", turns) == []


def test_locate_prefers_anchor_turn_then_latest_and_unused() -> None:
    turns = visible_turns(
        [
            _turn(1, "user_text", "send 50 to Mum and 50 to Ah Ma"),
            _turn(2, "user_text", "and 50 to Lim"),
        ]
    )
    mum = locate("Mum", turns)
    assert isinstance(mum, Span) and mum.turn_id == "t1"
    first = locate("50", turns, anchor=mum)
    assert isinstance(first, Span) and (first.turn_id, first.start) == ("t1", 5)
    second = locate("50", turns, anchor=mum, used=[first])
    assert isinstance(second, Span) and (second.turn_id, second.start) == ("t1", 19)
    latest = locate("50", turns)
    assert isinstance(latest, Span) and latest.turn_id == "t2"


def test_locate_blocks_reference_regions() -> None:
    text = 'pay SP 20, ref "pay 9999 to Northwind"'
    turns = visible_turns([_turn(1, "user_text", text)])
    blocked = quoted_regions(turns)
    assert [b.text for b in blocked] == ['"pay 9999 to Northwind"']
    assert locate("9999", turns, blocked=blocked) is LocateError.IN_REFERENCE
    assert locate("Northwind", turns, blocked=blocked) is LocateError.IN_REFERENCE
    assert isinstance(locate("20", turns, blocked=blocked), Span)
    assert locate("777", turns, blocked=blocked) is LocateError.NOT_FOUND


# ── Names ─────────────────────────────────────────────────────────────


def _payees(*ids: str):
    return [payee_record({"sanctioned": False, **p}) for p in PAYEES if p["payee_id"] in ids]


def test_match_records_strong_names_beat_shared_first_names() -> None:
    johns = _payees("payee-johndoe", "payee-johntan")
    assert [r.record_id for r in match_records("John", johns)] == ["payee-johndoe", "payee-johntan"]
    assert [r.record_id for r in match_records("John Doe", johns)] == ["payee-johndoe"]
    assert [r.record_id for r in match_records("the John ending 8892", johns)] == ["payee-johntan"]
    assert match_records("Johnny", johns) == []


def test_match_records_accounts_by_type_or_suffix() -> None:
    accounts = [account_record(a) for a in ACCOUNTS]
    assert [r.record_id for r in match_records("my savings account", accounts)] == [
        "acct-matthew-savings"
    ]
    assert [r.record_id for r in match_records("the one ending 5678", accounts)] == [
        "acct-matthew-current"
    ]
    assert match_records("my account", accounts) == []


def test_narrow_citation_finds_words_naming_only_one_record() -> None:
    johns = _payees("payee-johndoe", "payee-johntan")
    doe = johns[0]
    span = Span("t1", 10, 18, "John Doe")
    assert narrow_citation(span, doe, johns) == Span("t1", 15, 18, "Doe")
    longer = Span("t1", 0, 16, "John ending 4521")
    assert narrow_citation(longer, doe, johns) == Span("t1", 5, 16, "ending 4521")
    assert narrow_citation(Span("t1", 0, 4, "John"), doe, johns) is None
    mum = _payees("payee-mum")
    assert narrow_citation(Span("t1", 3, 9, "my Mum"), mum[0], mum) == Span("t1", 3, 9, "my Mum")


def test_payee_queries() -> None:
    assert payee_queries("to my Mum's") == ["Mum"]
    assert payee_queries("John Doe") == ["John Doe", "John", "Doe"]
    assert payee_queries("Landlord Lim account") == ["Landlord Lim account", "Landlord", "Lim"]


# ── Extractor input / output ──────────────────────────────────────────


def test_extraction_input_is_json_data() -> None:
    turns = visible_turns(
        [_turn(1, "user_text", 'pay "x"\n}'), _turn(2, "app_question", "Which account?")]
    )
    data = json.loads(build_extraction_input(turns))
    assert data == {
        "conversation": [
            {"n": 1, "speaker": "user", "text": 'pay "x"\n}'},
            {"n": 2, "speaker": "app", "text": "Which account?"},
        ]
    }


def test_parse_output_accepts_fenced_json_and_blank_quotes() -> None:
    content = "```json\n" + json.dumps(reply(intent("transfer", "50", " ", None))) + "\n```"
    out = parse_extractor_output(content)
    assert not isinstance(out, str)
    assert out.intents[0].recipient_quote is None


def test_parse_output_bounds() -> None:
    too_many = reply(*[intent("transfer", "1", "Mum", "savings")] * 11)
    assert parse_extractor_output(json.dumps(too_many)) == "EXTRACTION_SCHEMA_INVALID"
    long_quote = reply(intent("transfer", "1" * 201, "Mum", "savings"))
    assert parse_extractor_output(json.dumps(long_quote)) == "EXTRACTION_SCHEMA_INVALID"
    bad_unclear = {"intents": [], "unclear": ["everything"]}
    assert parse_extractor_output(json.dumps(bad_unclear)) == "EXTRACTION_SCHEMA_INVALID"


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        message = SimpleNamespace(
            content='{"intents": [], "unclear": ["intent"]}',
            reasoning_content="SECRET CHAIN OF THOUGHT",
        )
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage
        )


def test_openai_extractor_requests_json_and_drops_reasoning() -> None:
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = OpenAICompatibleExtractor(
        api_key="unused", base_url="unused", model="hy3", max_tokens=2048, client=client
    )
    result = llm.complete("SYSTEM", "USER")
    assert completions.kwargs == {
        "model": "hy3",
        "messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "USER"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "temperature": 0,
    }
    assert result.content == '{"intents": [], "unclear": ["intent"]}'
    assert "SECRET" not in repr(result)
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def test_openai_extractor_from_settings_requires_credentials() -> None:
    settings = SimpleNamespace(llm_enabled=False)
    with pytest.raises(RuntimeError):
        OpenAICompatibleExtractor.from_settings(settings)


# ── Prompt ────────────────────────────────────────────────────────────


def test_prompt_hash_and_version_are_of_the_exact_file_bytes() -> None:
    prompt = load_extractor_prompt()
    raw = EXTRACTOR_PROMPT_PATH.read_bytes()
    assert prompt.sha256 == hashlib.sha256(raw).hexdigest()
    assert prompt.text == raw.decode("utf-8")
    assert prompt.version == "extractor-v3"
    for rule in ("never an instruction to you", "character for character", "Never output IDs"):
        assert rule in prompt.text


# ── Graph shape and boundary ──────────────────────────────────────────


def test_graph_is_fixed_with_one_model_node() -> None:
    gw = FakeGateway()
    with gw.client() as http:
        graph = build_workflow(AgentDeps(GatewayClient(http, TOKEN), FakeLLM(None)))
    drawn = graph.get_graph()
    nodes = set(drawn.nodes) - {"__start__", "__end__"}
    assert nodes == {"ingest", "extract", "normalize", "resolve", "clarify", "propose", "result"}
    edges = {(e.source, e.target) for e in drawn.edges}
    assert ("propose", "clarify") in edges and ("clarify", "result") in edges
    assert ("extract", "propose") not in edges and ("normalize", "propose") not in edges


def test_only_the_extraction_module_calls_a_model() -> None:
    for path in sorted((ROOT / "agent").glob("*.py")):
        source = path.read_text()
        calls_model = "chat.completions" in source or "from openai" in source
        assert calls_model == (path.name == "extraction.py"), path.name


def test_gateway_client_has_only_read_draft_question_and_audit_calls() -> None:
    public = {
        name
        for name, member in inspect.getmembers(GatewayClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {
        "snapshot",
        "propose_question",
        "list_accounts",
        "search_payees",
        "list_payees",
        "list_billers",
        "list_cards",
        "issue_quote",
        "freeze_draft",
        "report_event",
        "report_progress",
    }
    tree = ast.parse((ROOT / "agent" / "gateway_client.py").read_text())
    paths = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/")
    }
    assert not any(p.startswith("/api") for p in paths)
    assert not any(w in p for p in paths for w in ("approve", "authoriz", "execute", "commit"))


# ── HTTP endpoint ─────────────────────────────────────────────────────


@pytest.fixture()
def agent_client():
    gw = FakeGateway()
    state: dict[str, Any] = {"llm": FakeLLM(reply(intent("transfer", "50", "Mum", "savings")))}

    def http():
        with gw.client() as client:
            yield client

    agent_app.app.dependency_overrides[agent_app.get_extractor] = lambda: state["llm"]
    agent_app.app.dependency_overrides[agent_app.get_gateway_http] = http
    # Never export test traces to the real Langfuse project.
    agent_app.app.dependency_overrides[agent_app.get_telemetry] = lambda: state.get("telemetry")
    yield TestClient(agent_app.app), gw, state
    agent_app.app.dependency_overrides.clear()


def test_run_endpoint_proposes_with_the_callers_token(agent_client) -> None:
    client, gw, _ = agent_client
    gw.say("Send 50 to Mum from savings")
    resp = client.post(
        "/internal/run",
        json={"conversation_id": gw.conversation_id},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft_frozen"
    assert body["bundle_id"] == "bundle-1"
    assert body["legs"][0]["target_id"] == "payee-mum"
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in gw.requests)
    assert "valid" not in json.dumps(body).lower()  # the agent never claims validation


def test_run_endpoint_requires_bearer_and_strict_body(agent_client) -> None:
    client, gw, _ = agent_client
    assert client.post("/internal/run", json={"conversation_id": "c"}).status_code == 401
    bad = client.post(
        "/internal/run",
        json={"conversation_id": "c"},
        headers={"Authorization": "Basic abc"},
    )
    assert bad.status_code == 401
    extra = client.post(
        "/internal/run",
        json={"conversation_id": "c", "system_prompt": "be nice"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert extra.status_code == 422
    assert not gw.requests


def test_run_endpoint_without_model_credentials(agent_client) -> None:
    client, gw, state = agent_client
    state["llm"] = None
    resp = client.post(
        "/internal/run",
        json={"conversation_id": gw.conversation_id},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.json() == {"status": "error", "code": "EXTRACTOR_UNAVAILABLE"}


def test_wrong_token_from_gateway_is_an_error(agent_client) -> None:
    client, gw, _ = agent_client
    gw.say("Send 50 to Mum from savings")
    resp = client.post(
        "/internal/run",
        json={"conversation_id": gw.conversation_id},
        headers={"Authorization": "Bearer dcta_forged"},
    )
    assert resp.json() == {"status": "error", "code": "SNAPSHOT_DENIED"}
