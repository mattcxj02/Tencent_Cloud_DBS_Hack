"""Gateway workflow telemetry (T-08): stages, correlation, audit trace IDs, outages.

Only ``Telemetry.for_testing`` (in-memory exporter, unroutable ``langfuse.invalid``) or
disabled facades are used; the first test proves the gateway facade under test is never
the ``.env`` backend. Telemetry is correlation only and never changes an outcome.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export import SpanExporter

from gateway import validation
from gateway.telemetry import _set_gateway_telemetry, get_gateway_telemetry
from gateway.validation import _set_validation_telemetry
from shared.settings import GatewaySettings
from shared.telemetry import Telemetry, parse_traceparent, pseudonymize
from tests.agent_support import intent, reply
from tests.orch_support import JOHN, JOHN_TEXT, MUM, MUM_TEXT, USER, Orch, o_fixture  # noqa: F401
from tests.t05_support import H, freeze, leg, say
from tests.t07_support import OP, OPERATOR_TOKEN, standalone, wrong
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import ScriptedLLM

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

LEVEL = "langfuse.observation.level"
MESSAGE = "langfuse.observation.status_message"
LINK_PREFIX = "http://langfuse.invalid/project/test-project/traces/"


@dataclass
class Tels:
    gateway: Telemetry
    agent: Telemetry
    validator: Telemetry


def _spans(tel: Telemetry) -> list[Any]:
    tel.flush()
    return list(tel.exporter.get_finished_spans())


def _named(spans: list[Any], name: str) -> list[Any]:
    return [s for s in spans if s.name == name]


def _one(spans: list[Any], name: str) -> Any:
    matches = _named(spans, name)
    assert len(matches) == 1, [s.name for s in spans]
    return matches[0]


def _trace(span: Any) -> str:
    return f"{span.context.trace_id:032x}"


def _span_id(span: Any) -> str:
    return f"{span.context.span_id:016x}"


def _meta(span: Any, key: str) -> Any:
    return span.attributes.get(f"langfuse.observation.metadata.{key}")


def _dump(spans: list[Any]) -> str:
    return json.dumps([{"name": s.name, **dict(s.attributes or {})} for s in spans], default=str)


def _audit(o: Orch, conv: str) -> dict[str, list[tuple[str | None, str | None]]]:
    out: dict[str, list[tuple[str | None, str | None]]] = {}
    for row in o.v.q(
        "SELECT step, trace_id, span_id FROM audit_events WHERE conversation_id = ? ORDER BY seq",
        (conv,),
    ):
        out.setdefault(row["step"], []).append((row["trace_id"], row["span_id"]))
    return out


@pytest.fixture(name="tels")
def tels_fixture(o: Orch) -> Iterator[Tels]:
    tels = Tels(
        Telemetry.for_testing("gateway"),
        Telemetry.for_testing("agent"),
        Telemetry.for_testing("validator"),
    )
    _set_gateway_telemetry(tels.gateway)
    _set_validation_telemetry(tels.gateway)  # one gateway facade for every stage
    o.agent_tel = tels.agent
    o.v.use_llm(ScriptedLLM.verdict("MATCH"), telemetry=tels.validator)
    try:
        yield tels
    finally:
        for tel in (tels.gateway, tels.agent, tels.validator):
            tel.shutdown()


# ── never the real backend ───────────────────────────────────────────


def test_gateway_telemetry_under_test_is_never_the_real_backend() -> None:
    tel = get_gateway_telemetry()
    assert tel._backend is None and not tel.enabled
    assert tel.health()["state"] == "disabled"
    # Validation (and anything else that asks for it) resolves to this same facade.
    assert validation._telemetry is None
    assert validation.get_validation_telemetry() is tel
    # Settings that ignore .env never configure Langfuse.
    assert not Telemetry(GatewaySettings(_env_file=None), "gateway").enabled


def test_injected_facade_points_at_an_unroutable_host(o: Orch, tels: Tels) -> None:
    for tel in (get_gateway_telemetry(), validation.get_validation_telemetry()):
        assert tel is tels.gateway
        assert tel._backend is not None and tel._backend.base_url == "http://langfuse.invalid"


# ── orchestration: one trace across gateway, agent and validator ─────


def test_draft_is_one_trace_across_gateway_agent_and_validator(o: Orch, tels: Tels) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    body = o.drafted(conv)
    assert body["status"] == "ready"

    gateway = _spans(tels.gateway)
    draft = _one(gateway, "gateway.draft")
    trace = _trace(draft)
    for name in ("agent.call", "compile", "semantic_validation"):
        assert _trace(_one(gateway, name)) == trace, name
    assert _trace(_one(_spans(tels.agent), "agent.run")) == trace
    validator_spans = _spans(tels.validator)
    assert validator_spans and all(_trace(s) == trace for s in validator_spans)
    assert _trace(_one(gateway, "ingest")) != trace  # each user turn is its own trace

    # The agent call carried this trace (and nothing else) as traceparent.
    sent = parse_traceparent(o.agent_requests[0].headers["traceparent"])
    assert sent is not None and sent.trace_id == trace
    assert sent.span_id == _span_id(_one(gateway, "agent.call"))

    # Pseudonymous user, session = conversation, allowlisted correlation IDs.
    assert draft.attributes["session.id"] == conv
    assert draft.attributes["user.id"] == pseudonymize(USER)
    assert USER not in _dump(gateway)
    assert _meta(draft, "bundle_id") == body["bundle_id"]
    assert _meta(draft, "validator_verdict") == "MATCH"
    assert _meta(draft, "amount") == "50.00"
    assert MUM_TEXT not in _dump(gateway)  # turn text never reaches telemetry

    # Audit rows carry the same correlation IDs.
    audit = _audit(o, conv)
    compile_span = _one(gateway, "compile")
    assert audit["gateway.draft.freeze"] == [(trace, _span_id(compile_span))]
    assert {t for t, _ in audit["gateway.validation.match"]} == {trace}
    assert {t for t, _ in audit["agent.propose"]} == {trace}
    ingest = _one(gateway, "ingest")
    assert audit["gateway.conversation.revision"] == [(_trace(ingest), _span_id(ingest))]
    assert body["validation"]["trace_id"] == trace


def test_challenge_approval_execution_and_receipt_spans(o: Orch, tels: Tels) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    ready = o.drafted(conv)
    view = o.v.issue(ready["bundle_id"]).json()
    approved = o.v.approve(view)
    assert approved.json()["status"] == "COMMITTED", approved.text

    gateway = _spans(tels.gateway)
    draft_trace = _trace(_one(gateway, "gateway.draft"))
    challenge = _one(gateway, "challenge")
    approval = _one(gateway, "approval")
    execution = _one(gateway, "execution")
    receipt = _one(gateway, "receipt")
    policies = _named(gateway, "policy")
    assert len(policies) == 3  # issuance pre-check, post-signature pre-check, commit recheck

    assert _trace(challenge) != _trace(approval) != draft_trace
    assert _meta(challenge, "linked_trace_id") == draft_trace
    assert _meta(challenge, "authorization_id") == view["authorization_id"]
    assert _meta(challenge, "envelope_hash") == view["envelope_hash"]
    assert challenge.attributes["session.id"] == conv
    assert {_trace(p) for p in policies} == {_trace(challenge), _trace(approval)}
    assert all(_meta(p, "policy_decision") == "PASS" for p in policies)
    for span in (execution, receipt):
        assert _trace(span) == _trace(approval)
    assert _meta(approval, "approval_state") == "COMMITTED"
    assert _meta(receipt, "amount") == "50.00" and _meta(receipt, "leg_count") == 1
    # The challenge span closed before the person was asked to approve.
    assert challenge.end_time <= approval.start_time

    audit = _audit(o, conv)
    assert {t for t, _ in audit["gateway.authorization.create"]} == {_trace(challenge)}
    assert {t for t, _ in audit["gateway.approval.signature_verified"]} == {_trace(approval)}
    assert audit["gateway.execution.commit"] == [(_trace(approval), _span_id(execution))]

    dumped = _dump(gateway)
    for secret in ("clientDataJSON", "authenticatorData", '"signature"', "public_key", "nonce"):
        assert secret not in dumped

    evidence = o.evidence(conv).json()
    for trace in (draft_trace, _trace(challenge), _trace(approval)):
        assert LINK_PREFIX + trace in evidence["trace_urls"]
    assert all(url.startswith(LINK_PREFIX) for url in evidence["trace_urls"])
    assert evidence["drafts"][0]["validation"]["trace_url"] == LINK_PREFIX + draft_trace


def test_step_up_spans_never_carry_the_code(
    o: Orch, tels: Tels, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    conv = o.conversation("Send 600 bucks to Mum from my savings")
    o.extractor(reply(intent("transfer", "600 bucks", "Mum", "savings")))
    view = o.v.issue(o.drafted(conv)["bundle_id"]).json()
    assert o.v.approve(view).json()["status"] == "AWAITING_STEP_UP"
    authz = view["authorization_id"]
    code = o.client.post(f"/operator/step-up/{authz}/deliver", headers=OP).json()["code"]
    url = f"/api/authorizations/{authz}/step-up"
    assert o.client.post(url, json={"code": wrong(code)}, headers=H).status_code == 409
    done = o.client.post(url, json={"code": code}, headers=H)
    assert done.json()["status"] == "COMMITTED", done.text

    gateway = _spans(tels.gateway)
    approval = _one(gateway, "approval")
    assert approval.attributes[LEVEL] == "DEFAULT"
    assert approval.attributes[MESSAGE].startswith("pending: ")
    assert _meta(approval, "approval_state") == "AWAITING_STEP_UP"
    entered, rejected, passed = _named(gateway, "step_up")
    assert entered.attributes[MESSAGE].startswith("pending:")
    assert _trace(entered) == _trace(approval)
    assert rejected.attributes[MESSAGE] == "rejected: STEP_UP_CODE_INVALID"
    assert rejected.attributes[LEVEL] == "WARNING"
    assert _meta(passed, "outcome") == "ok" and MESSAGE not in passed.attributes
    assert _trace(_named(gateway, "receipt")[0]) == _trace(passed)
    assert passed.attributes["session.id"] == conv
    assert not standalone(code, _dump(gateway) + _dump(_spans(tels.agent)))
    commit = _audit(o, conv)["gateway.execution.commit"]
    assert {t for t, _ in commit} == {_trace(passed)}


# ── expected outcomes are statuses, not errors ───────────────────────


def test_expected_outcomes_are_statuses_not_errors(o: Orch, tels: Tels) -> None:
    # Clarification
    conv = o.conversation(JOHN_TEXT)
    o.extractor(JOHN)
    assert o.drafted(conv)["status"] == "clarify"
    # Hard-policy block at envelope issuance (per-leg mandate limit 1,000)
    big = o.conversation("Send $1500 to Mum from my savings")
    o.extractor(reply(intent("transfer", "$1500", "Mum", "savings")))
    ready = o.drafted(big)
    assert o.v.issue(ready["bundle_id"]).json()["detail"] == "POLICY_BLOCK"
    # Stale proposal at freeze
    turn = say(o.env, big, "Send 50 to Mum from my savings")
    stale = freeze(
        o.env,
        big,
        [leg(turn, amount="50.00", amount_words="50", target_id="payee-mum",
             target_words="Mum", source_words="savings")],
        rev=1,
    )  # fmt: skip
    assert stale.json()["detail"] == "STALE_CONVERSATION_REVISION"
    # Agent timeout
    o.handler = lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    assert o.draft(big).json() == {"status": "error", "code": "AGENT_TIMEOUT"}

    gateway = _spans(tels.gateway)
    drafts = _named(gateway, "gateway.draft")
    assert drafts[0].attributes[LEVEL] == "DEFAULT"
    assert drafts[0].attributes[MESSAGE] == "clarification: QUESTION_ASKED"
    assert drafts[-1].attributes[MESSAGE] == "timeout: AGENT_TIMEOUT"
    assert drafts[-1].attributes[LEVEL] == "WARNING"
    challenge = _one(gateway, "challenge")
    assert challenge.attributes[LEVEL] == "WARNING"
    assert challenge.attributes[MESSAGE].startswith("blocked: ")
    blocked_policy = [
        p for p in _named(gateway, "policy") if _meta(p, "policy_decision") == "BLOCK"
    ]
    assert blocked_policy and blocked_policy[0].attributes[MESSAGE].startswith("blocked: ")
    compiles = _named(gateway, "compile")
    assert compiles[-1].attributes[MESSAGE] == "superseded: STALE_CONVERSATION_REVISION"
    expected = ("gateway.draft", "challenge", "policy", "compile", "agent.call")
    for span in gateway:
        if span.name in expected:
            assert span.attributes.get(LEVEL) != "ERROR", (span.name, span.attributes)


# ── outages never change outcomes ────────────────────────────────────


class _BrokenExporter(SpanExporter):
    def export(self, spans: Any) -> Any:
        raise RuntimeError("langfuse is down")

    def shutdown(self) -> None:
        return None


def test_failing_exporter_changes_no_outcome_and_degrades_health(o: Orch) -> None:
    tels = [
        Telemetry.for_testing(s, exporter=_BrokenExporter())
        for s in ("gateway", "agent", "validator")
    ]
    gateway, agent, validator = tels
    _set_gateway_telemetry(gateway)
    _set_validation_telemetry(gateway)
    o.agent_tel = agent
    o.v.use_llm(ScriptedLLM.verdict("MATCH"), telemetry=validator)
    try:
        before = o.v.balance()
        conv = o.conversation(MUM_TEXT)
        o.extractor(MUM)
        ready = o.drafted(conv)
        assert ready["status"] == "ready"
        view = o.v.issue(ready["bundle_id"]).json()
        first = o.v.approve(view).json()
        assert first["status"] == "COMMITTED" and first["replayed"] is False
        again = o.v.approve(view).json()  # a retry after the outage returns the stored receipt
        assert again["status"] == "COMMITTED" and again["receipt_id"] == first["receipt_id"]
        assert o.v.balance() == before - 5000  # moved exactly once
        assert o.evidence(conv).json()["audit"]["ok"] is True

        gateway.flush()
        health = o.client.get("/health").json()["telemetry"]
        assert health["state"] == "degraded" and health["service"] == "gateway"
        assert "Bearer" not in json.dumps(health)
    finally:
        for tel in tels:
            tel.shutdown()


def test_health_reports_disabled_telemetry_without_secrets(o: Orch) -> None:
    body = o.client.get("/health").json()
    assert body["telemetry"]["state"] == "disabled"
    assert body["telemetry"]["backend"] == "langfuse"
    text = json.dumps(body)
    assert "sk-" not in text and "pk-lf" not in text and "REDACTED" not in text
