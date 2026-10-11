"""End to end through the browser routes: You said → We clarified → You approved → Executed.

Real gateway, real agent app and real validator app in-process; only the two LLMs are
fakes, and telemetry goes to in-memory exporters (``Telemetry.for_testing``). The
passkey is a software authenticator producing real WebAuthn assertions.
"""

from __future__ import annotations

import pytest

from gateway.telemetry import _set_gateway_telemetry
from gateway.validation import _set_validation_telemetry
from shared.telemetry import Telemetry
from tests.agent_support import FakeLLM, intent, reply
from tests.orch_support import JOHN, JOHN_TEXT, Orch, o_fixture  # noqa: F401
from tests.t05_support import H
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import ScriptedLLM

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def test_conversation_to_receipt_with_evidence(o: Orch) -> None:
    tels = [Telemetry.for_testing(s) for s in ("gateway", "agent", "validator")]
    gateway_tel, agent_tel, validator_tel = tels
    _set_gateway_telemetry(gateway_tel)
    _set_validation_telemetry(gateway_tel)
    o.agent_tel = agent_tel
    o.v.use_llm(ScriptedLLM.verdict("MATCH"), telemetry=validator_tel)
    try:
        savings_before = o.v.balance()

        # You said
        conv = o.client.post("/api/conversations", json={}).json()["conversation_id"]
        said = o.client.post(f"/api/conversations/{conv}/text", json={"text": JOHN_TEXT})
        assert said.status_code == 200, said.text

        # We clarified
        o.llm = FakeLLM(JOHN, reply(intent("transfer", "$50", "John Doe", "savings")))
        clarify = o.drafted(conv)
        assert clarify["status"] == "clarify"
        answer = o.client.post(
            f"/api/conversations/{conv}/answer",
            json={"answers_question_id": clarify["question"]["turn_id"], "text": "John Doe"},
        )
        assert answer.status_code == 200, answer.text
        ready = o.drafted(conv)
        assert ready["status"] == "ready", ready
        assert ready["total_debit"] == "50.00"
        assert ready["legs"][0]["display_name"] == "John Doe"
        assert ready["legs"][0]["source_display"] == "Savings ••1234"

        # You approved (the envelope the UI renders comes from the gateway)
        issued = o.client.post(
            f"/api/drafts/{ready['bundle_id']}/authorizations",
            json={"credential_id": o.v.cred.credential_id_b64},
            headers=H,
        )
        assert issued.status_code == 200, issued.text
        view = issued.json()
        assert view["envelope"]["payload"]["bundle_id"] == ready["bundle_id"]
        approved = o.v.approve(view)
        assert approved.status_code == 200, approved.text

        # Executed
        body = approved.json()
        assert body["status"] == "COMMITTED" and body["replayed"] is False
        receipt = o.client.get(f"/api/authorizations/{view['authorization_id']}/receipt").json()
        assert receipt["receipt_id"] == body["receipt_id"]
        assert receipt["receipt"]["total_debit"] == "50.00"
        assert o.v.balance() == savings_before - 5000

        evidence = o.client.get(f"/api/conversations/{conv}/evidence").json()
        roles = [t["role"] for t in evidence["turns"]]
        assert roles == ["user_text", "app_question", "user_answer"]
        assert evidence["turns"][1]["text"] == clarify["question"]["text"]
        assert evidence["turns"][2]["answers_question_id"] == clarify["question"]["turn_id"]
        [draft] = evidence["drafts"]
        assert (draft["bundle_id"], draft["status"]) == (ready["bundle_id"], "EXECUTED")
        assert draft["validation"]["verdict"] == "MATCH"
        assert draft["validation"]["trace_url"] is not None
        [authz] = evidence["authorizations"]
        assert authz["status"] == "COMMITTED"
        assert authz["approval"]["credential_id"] == o.v.cred.credential_id_b64
        assert authz["approval"]["user_verified"] is True
        assert authz["step_up"] is None
        assert authz["receipt"] == receipt["receipt"]
        assert evidence["audit"]["ok"] is True
        steps = [e["step"] for e in evidence["audit"]["events"]]
        for stage in (
            "gateway.conversation.revision",  # you said / answered
            "agent.clarify",  # we clarified
            "gateway.draft.freeze",
            "gateway.validation.match",
            "gateway.authorization.create",
            "gateway.approval.signature_verified",  # you approved
            "gateway.execution.commit",  # executed
        ):
            assert stage in steps, stage
        assert evidence["trace_urls"]
        assert all(u.startswith("http://langfuse.invalid/") for u in evidence["trace_urls"])
        gateway_events = [e for e in evidence["audit"]["events"] if e["source"] == "gateway"]
        assert all(
            e["trace_id"] for e in gateway_events if e["step"] != "gateway.conversation.create"
        )
    finally:
        for tel in tels:
            tel.shutdown()
