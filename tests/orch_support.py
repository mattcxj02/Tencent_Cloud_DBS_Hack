"""Harness for the gateway orchestration, read-route and gateway-telemetry tests.

Built on the T-10 ``v`` fixture (real gateway routes, real validation coordinator and
eligibility hook, an in-process validator app with a scripted LLM, software passkey,
frozen gateway clock). The orchestrator reaches the agent through
``gateway.orchestration._set_agent_http``: an ``httpx.MockTransport`` that records each
request and forwards it to the REAL agent FastAPI app (``agent.app``) with three
dependency overrides — a fake extractor LLM, the gateway's own ``TestClient`` as the
agent's HTTP client (with the traceparent hook, like production) and an injected agent
telemetry facade. A test can replace the forwarder with any handler (errors, timeouts,
malformed replies). Every telemetry facade here is disabled or ``Telemetry.for_testing``;
none is built from ``.env``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.app import app as agent_app
from agent.app import get_extractor, get_gateway_http, get_telemetry
from gateway.app import app as gateway_app
from gateway.orchestration import _set_agent_http
from shared.settings import AgentSettings, GatewaySettings
from shared.telemetry import Telemetry, httpx_request_hook
from tests.agent_support import FakeLLM, intent, reply
from tests.t05_support import H, new_conversation, say
from tests.t10_support import V, disabled_telemetry, v_fixture  # noqa: F401
from tests.validator_fakes import TOKEN

USER = "user-matthew"
AGENT_SCOPES = {
    "conversation:read",
    "bank:read",
    "draft:freeze",
    "conversation:propose_question",
    "audit:append_agent",
}

Handler = Callable[[httpx.Request], httpx.Response]

#: Mum transfer and card-balance payment: legs of 50.00 and 642.30 (total 692.30).
TWO_LEGS_TEXT = "Send fifty bucks to Mum from my savings and pay my card balance from current"
TWO_LEGS = reply(
    intent("transfer", "fifty bucks", "Mum", "savings"),
    intent("pay_card", "card balance", "card", "current"),
)
MUM_TEXT = "Send fifty bucks to Mum from my savings"
MUM = reply(intent("transfer", "fifty bucks", "Mum", "savings"))
JOHN_TEXT = "Send $50 to John from my savings"
JOHN = reply(intent("transfer", "$50", "John", "savings"))


@dataclass
class Orch:
    v: V
    llm: FakeLLM = field(default_factory=lambda: FakeLLM(MUM))
    agent_tel: Telemetry = field(default_factory=lambda: disabled_telemetry("agent"))
    agent_requests: list[httpx.Request] = field(default_factory=list)
    handler: Handler | None = None
    forward: Handler | None = None

    @property
    def env(self):
        return self.v.env

    @property
    def client(self) -> TestClient:
        return self.v.env.client

    def extractor(self, *replies: Any) -> None:
        self.llm = FakeLLM(*replies)

    def conversation(self, text: str | None = None) -> str:
        conv = new_conversation(self.env)
        if text is not None:
            say(self.env, conv, text)
        return conv

    def draft(self, conv: str, **kw: Any) -> httpx.Response:
        kw.setdefault("headers", H)
        return self.client.post(f"/api/conversations/{conv}/draft", **kw)

    def drafted(self, conv: str) -> dict[str, Any]:
        resp = self.draft(conv)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def evidence(self, conv: str) -> httpx.Response:
        return self.client.get(f"/api/conversations/{conv}/evidence")

    def last_token_payload(self) -> dict[str, Any]:
        """The delegation token the gateway sent with the last agent call, decoded."""
        token = self.agent_requests[-1].headers["authorization"].removeprefix("Bearer ")
        body = token.removeprefix("dcta_").split(".")[0]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def agent_response(request: httpx.Request, outcome: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=outcome, request=request)


@pytest.fixture(name="o")
def o_fixture(v: V, monkeypatch: pytest.MonkeyPatch) -> Iterator[Orch]:
    import gateway.readiness as readiness

    monkeypatch.setattr(
        readiness, "load_gateway_settings",
        lambda: GatewaySettings(_env_file=None, VALIDATOR_SERVICE_TOKEN=TOKEN),
    )
    monkeypatch.setattr(
        "agent.app.load_agent_settings",
        lambda: AgentSettings(_env_file=None, tokenhub_key="test-extractor-key"),
    )
    harness = Orch(v=v)
    gateway_for_agent = TestClient(gateway_app)
    gateway_for_agent.event_hooks = {"request": [httpx_request_hook], "response": []}
    agent_app.dependency_overrides[get_extractor] = lambda: harness.llm
    agent_app.dependency_overrides[get_gateway_http] = lambda: gateway_for_agent
    agent_app.dependency_overrides[get_telemetry] = lambda: harness.agent_tel
    agent_client = TestClient(agent_app)

    def real_agent(request: httpx.Request) -> httpx.Response:
        headers = {
            k: request.headers[k]
            for k in ("authorization", "traceparent", "content-type", "x-dcta-deadline")
            if k in request.headers
        }
        resp = agent_client.request(
            request.method, request.url.path, content=request.content, headers=headers
        )
        return httpx.Response(resp.status_code, content=resp.content, request=request)

    harness.forward = real_agent

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return real_agent(request)
        harness.agent_requests.append(request)
        return (harness.handler or real_agent)(request)

    _set_agent_http(
        httpx.Client(base_url="http://agent.test", transport=httpx.MockTransport(transport))
    )
    try:
        yield harness
    finally:
        _set_agent_http(None)
        agent_app.dependency_overrides.clear()
