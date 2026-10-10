"""T-10 telemetry: semantic_validation spans and the validator-llm-call generation.

Only ``Telemetry.for_testing`` (in-memory exporter, unroutable URL) or disabled
facades are used, and every settings object here ignores ``.env``; the last test proves
that. Telemetry is correlation only and never changes a verdict or an authorization.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway import validation
from gateway.validation import _set_validation_telemetry
from shared.canonical import canonical_bytes
from shared.settings import GatewaySettings
from shared.telemetry import Telemetry, parse_traceparent
from tests.t10_support import V, assert_test_telemetry, ok_reply, v_fixture  # noqa: F401
from tests.validator_fakes import (
    AUTH,
    TOKEN,
    ScriptedLLM,
    leg,
    make_settings,
    request_dict,
    simple_request,
    turn,
)
from validator.app import PROMPT_VERSION
from validator.app import create_app as create_validator_app
from validator.prompt import PROMPT_HASH, SYSTEM_PROMPT

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def _exported(tel: Telemetry) -> list[Any]:
    tel.flush()
    return list(tel.exporter.get_finished_spans())


def _one(spans: list[Any], name: str) -> Any:
    matches = [s for s in spans if s.name == name]
    assert len(matches) == 1, [s.name for s in spans]
    return matches[0]


def _meta(span: Any, key: str) -> Any:
    return span.attributes.get(f"langfuse.observation.metadata.{key}")


def _trace(span: Any) -> str:
    return f"{span.context.trace_id:032x}"


def _dump(spans: list[Any]) -> str:
    return json.dumps(
        [
            {"name": s.name, **dict(s.attributes or {}), "status": s.status.description}
            for s in spans
        ],
        default=str,
        ensure_ascii=False,
    )


def test_validation_is_one_trace_across_gateway_and_validator(v: V) -> None:
    gateway_tel = Telemetry.for_testing("gateway")
    validator_tel = Telemetry.for_testing("validator")
    try:
        _set_validation_telemetry(gateway_tel)
        v.use_llm(ScriptedLLM.verdict("MATCH"), telemetry=validator_tel)
        _, bid = v.mum()
        view = v.validated(bid)
        assert view["eligible_for_confirmation"]

        gateway_spans, validator_spans = _exported(gateway_tel), _exported(validator_tel)
        gspan = _one(gateway_spans, "semantic_validation")
        vspan = _one(validator_spans, "semantic_validation")
        gen = _one(validator_spans, "validator-llm-call")

        trace_id = _trace(gspan)
        assert view["trace_id"] == trace_id == _trace(vspan) == _trace(gen)
        assert vspan.parent.span_id == gspan.context.span_id
        assert gen.parent.span_id == vspan.context.span_id

        assert _meta(gspan, "stage") == "semantic_validation"
        assert _meta(gspan, "bundle_id") == bid and _meta(gspan, "attempt") == 1
        assert _meta(gspan, "validator_verdict") == "MATCH"
        assert gen.attributes["langfuse.observation.type"] == "generation"
        assert gen.attributes["langfuse.observation.model.name"] == "fake-model"
        assert _meta(gen, "prompt_hash") == PROMPT_HASH
        assert _meta(gen, "prompt_version") == PROMPT_VERSION == "validator-v1"
        assert _meta(gen, "validator_verdict") == "MATCH"
        assert _meta(gen, "usage_status") == "unavailable"  # the fake reports no usage
        assert isinstance(_meta(gen, "latency_ms"), int | float)

        # The trace ID is stored with the result and with the audit events.
        assert v.results(bid)[0]["trace_id"] == trace_id
        audit = v.q(
            "SELECT step, trace_id FROM audit_events WHERE step LIKE 'gateway.validation.%'"
        )
        assert {r["step"] for r in audit} == {
            "gateway.validation.requested",
            "gateway.validation.match",
        }
        assert {r["trace_id"] for r in audit} == {trace_id}

        # No secrets, raw account IDs or system prompt text reach the exporter.
        dump = _dump(gateway_spans + validator_spans)
        for forbidden in (
            TOKEN,
            "Bearer",
            "acct-matthew-savings",
            "acct-matthew-current",
            "user-matthew",
            SYSTEM_PROMPT[:80],
        ):
            assert forbidden not in dump
        assert "Send 50 to Mum" in dump  # the synthetic conversation the model checked
    finally:
        gateway_tel.shutdown()
        validator_tel.shutdown()


def test_gateway_sends_traceparent_and_never_a_session_or_baggage(v: V) -> None:
    tel = Telemetry.for_testing("gateway")
    try:
        _set_validation_telemetry(tel)
        v.use_transport(ok_reply)
        _, bid = v.mum()
        view = v.validated(bid)
        (sent,) = v.sent
        parent = parse_traceparent(sent.headers["traceparent"])
        assert parent is not None and parent.trace_id == view["trace_id"]
        assert not {"baggage", "cookie"} & {k.lower() for k in sent.headers}
    finally:
        tel.shutdown()


def test_validator_continues_the_callers_trace_from_traceparent() -> None:
    tel = Telemetry.for_testing("validator")
    try:
        client = TestClient(
            create_validator_app(
                settings=make_settings(), llm=ScriptedLLM.verdict("MATCH"), telemetry=tel
            )
        )
        header = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
        resp = client.post(
            "/internal/validate",
            content=canonical_bytes(simple_request()),
            headers={**AUTH, "traceparent": header},
        )
        assert resp.status_code == 200 and resp.json()["verdict"] == "MATCH"
        spans = _exported(tel)
        assert sorted(s.name for s in spans) == ["semantic_validation", "validator-llm-call"]
        assert {_trace(s) for s in spans} == {"ab" * 16}
        root = _one(spans, "semantic_validation")
        assert f"{root.parent.span_id:016x}" == "cd" * 8 and root.parent.is_remote
    finally:
        tel.shutdown()


def test_refusals_are_traced_without_a_model_generation() -> None:
    tel = Telemetry.for_testing("validator")
    llm = ScriptedLLM.verdict("MATCH")
    try:
        client = TestClient(create_validator_app(settings=make_settings(), llm=llm, telemetry=tel))
        body = request_dict(
            [turn("turn-1", 1, "Send 50 dollars to Mum")],
            [leg("leg-1", "transfer", "payee-mum", "50.00")],
        )
        body["bundle"]["conversation_revision"] = 7  # bound to another revision
        resp = client.post("/internal/validate", content=json.dumps(body), headers=AUTH)
        assert resp.json()["code"] == "BINDING_MISMATCH"
        assert client.post("/internal/validate", content=b"{}").status_code == 401

        spans = _exported(tel)
        assert [s.name for s in spans] == ["semantic_validation"]  # none for the 401
        assert spans[0].attributes["langfuse.observation.status_message"] == (
            "rejected: BINDING_MISMATCH"
        )
        assert llm.calls == []
    finally:
        tel.shutdown()


class _BrokenExporter(SpanExporter):
    def export(self, spans: Any) -> Any:
        raise RuntimeError("langfuse is down")

    def shutdown(self) -> None:
        return None


def test_telemetry_outage_changes_neither_verdict_nor_authorization(v: V) -> None:
    gateway_tel = Telemetry.for_testing("gateway", exporter=_BrokenExporter())
    validator_tel = Telemetry.for_testing("validator", exporter=_BrokenExporter())
    try:
        _set_validation_telemetry(gateway_tel)
        v.use_llm(ScriptedLLM.verdict("MATCH"), telemetry=validator_tel)
        _, bid = v.mum()
        assert v.validated(bid)["eligible_for_confirmation"]
        assert v.issue(bid).status_code == 200
        gateway_tel.flush()
        assert gateway_tel.health()["state"] == "degraded"
    finally:
        gateway_tel.shutdown()
        validator_tel.shutdown()


def test_disabled_telemetry_sends_no_trace_header_and_changes_nothing(v: V) -> None:
    v.use_transport(ok_reply)
    _, bid = v.mum()
    view = v.validated(bid)
    assert "traceparent" not in v.sent[0].headers
    assert view["trace_id"] is None and view["eligible_for_confirmation"]


def test_tests_never_configure_a_real_langfuse_backend(v: V) -> None:
    # Test settings ignore .env, so they carry no Langfuse keys and build disabled
    # facades; this is the facade the validator app builds when none is injected.
    validator_settings = make_settings()
    gateway_settings = GatewaySettings(_env_file=None)
    assert not validator_settings.langfuse_enabled and not gateway_settings.langfuse_enabled
    for settings, service in ((validator_settings, "validator"), (gateway_settings, "gateway")):
        tel = Telemetry(settings, service)
        assert not tel.enabled and tel.health()["state"] == "disabled"

    # The gateway validation facade under test is the fixture's disabled one.
    assert validation._telemetry is not None and not validation._telemetry.enabled
    assert_test_telemetry(validation._telemetry)

    # In-memory facades keep spans in the process and point at an unroutable host.
    tel = Telemetry.for_testing("gateway")
    try:
        assert isinstance(tel.exporter, InMemorySpanExporter)
        assert_test_telemetry(tel)
    finally:
        tel.shutdown()
