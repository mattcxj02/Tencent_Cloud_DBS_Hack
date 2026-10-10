"""T-09 agent observations through the T-08 telemetry facade (in-memory exporter only)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent.gateway_client import GatewayClient
from agent.prompt import load_extractor_prompt
from agent.workflow import AgentDeps, run_agent
from shared.telemetry import Telemetry, parse_traceparent
from tests.agent_support import TOKEN, FakeGateway, FakeLLM, intent, reply

META = "langfuse.observation.metadata."


@pytest.fixture()
def telemetry() -> Iterator[Telemetry]:
    tel = Telemetry.for_testing("agent")
    yield tel
    tel.shutdown()


def _run(gw: FakeGateway, llm: FakeLLM, tel: Telemetry, **kwargs):  # type: ignore[no-untyped-def]
    with gw.client() as http:
        out = run_agent(
            gw.conversation_id, AgentDeps(GatewayClient(http, TOKEN), llm, telemetry=tel), **kwargs
        )
    tel.flush()
    return out, {s.name: s for s in tel.exporter.get_finished_spans()}  # type: ignore[union-attr]


def test_one_trace_with_stage_spans_and_extractor_generation(telemetry: Telemetry) -> None:
    gw = FakeGateway()
    gw.say("Send $50 to Mum from savings")
    out, spans = _run(gw, FakeLLM(reply(intent("transfer", "$50", "Mum", "savings"))), telemetry)

    assert out["status"] == "draft_frozen"
    assert {
        "agent.run",
        "ingest",
        "extract",
        "normalize",
        "resolve",
        "propose",
        "extractor",
    } <= set(spans)
    trace_ids = {f"{s.context.trace_id:032x}" for s in spans.values()}
    assert trace_ids == {out["trace_id"]}

    gen = spans["extractor"].attributes
    prompt = load_extractor_prompt()
    assert gen["langfuse.observation.type"] == "generation"
    assert gen[META + "prompt_hash"] == prompt.sha256
    assert gen[META + "prompt_version"] == prompt.version
    assert gen[META + "model"] == "fake-extractor"
    assert gen[META + "usage_status"] == "reported"
    assert spans["agent.run"].attributes[META + "bundle_id"] == out["bundle_id"]

    # The system prompt, tokens and reasoning never leave the process.
    exported = " ".join(str(v) for s in spans.values() for v in s.attributes.values())
    assert "You only copy words" not in exported
    assert TOKEN not in exported and "Bearer" not in exported
    assert "reasoning" not in exported

    # Agent audit events carry the trace for the evidence viewer.
    assert {e["trace_id"] for e in gw.events} == {out["trace_id"]}
    assert all(e.get("span_id") for e in gw.events)


def test_clarification_and_errors_are_not_reported_as_success(telemetry: Telemetry) -> None:
    gw = FakeGateway()
    gw.say("Send $50 to John from savings")
    out, spans = _run(gw, FakeLLM(reply(intent("transfer", "$50", "John", "savings"))), telemetry)
    assert out["status"] == "clarify"
    for name in ("resolve", "clarify", "agent.run"):
        assert spans[name].attributes[META + "outcome"] == "clarification"
        assert spans[name].attributes[META + "reason_code"] == "RECIPIENT_AMBIGUOUS"


def test_malformed_model_output_marks_generation_as_error(telemetry: Telemetry) -> None:
    gw = FakeGateway()
    gw.say("Send $50 to Mum from savings")
    out, spans = _run(gw, FakeLLM("not json"), telemetry)
    assert out["status"] == "error"
    assert spans["extractor"].attributes[META + "outcome"] == "error"
    assert spans["extractor"].attributes[META + "reason_code"] == "EXTRACTION_MALFORMED"
    assert spans["extract"].attributes["langfuse.observation.level"] == "ERROR"


def test_continues_the_gateway_trace(telemetry: Telemetry) -> None:
    parent = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert parent is not None
    gw = FakeGateway()
    gw.say("Send $50 to Mum from savings")
    out, spans = _run(
        gw, FakeLLM(reply(intent("transfer", "$50", "Mum", "savings"))), telemetry, parent=parent
    )
    assert out["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert f"{spans['agent.run'].parent.span_id:016x}" == "00f067aa0ba902b7"


def test_no_telemetry_means_no_trace_id() -> None:
    gw = FakeGateway()
    gw.say("Send $50 to Mum from savings")
    with gw.client() as http:
        out = run_agent(
            gw.conversation_id,
            AgentDeps(
                GatewayClient(http, TOKEN),
                FakeLLM(reply(intent("transfer", "$50", "Mum", "savings"))),
            ),
        )
    assert "trace_id" not in out
    assert all("trace_id" not in e for e in gw.events)
