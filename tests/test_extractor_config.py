"""Phase 2: supported reasoning controls, fail-closed output and reused clients."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent import app as agent_app
from agent.extraction import OpenAICompatibleExtractor, extract_intents
from agent.gateway_client import GatewayClient
from agent.prompt import load_extractor_prompt
from agent.workflow import AgentDeps, run_agent
from shared.settings import AgentSettings, ValidatorSettings
from shared.telemetry import Telemetry
from tests.agent_support import TOKEN, FakeGateway, intent, reply


class Completions:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=self.content, reasoning_content="PRIVATE REASONING"
                ),
                finish_reason=self.finish_reason,
            )],
            usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=30, total_tokens=130,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


def extractor(completions: Completions, **kwargs) -> OpenAICompatibleExtractor:
    return OpenAICompatibleExtractor(
        api_key="unused", base_url="unused", model=kwargs.pop("model", "hy3"),
        max_tokens=kwargs.pop("max_tokens", 4096),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)), **kwargs,
    )


@pytest.mark.parametrize(
    ("provider", "model", "effort", "body"),
    [
        ("tokenhub", "hy3", "no_think", {"reasoning_effort": "no_think"}),
        ("tokenhub", "hy3", "low", {"reasoning_effort": "low"}),
        ("tokenhub", "hy3", "high", {"reasoning_effort": "high"}),
        ("tokenhub", "mimo-v2.6-flash", "no_think", {"thinking": {"type": "disabled"}}),
        ("tokenhub", "hy3", None, None),
        ("tokenhub", "mimo-v2.6-flash", None, None),
        ("tokenhub", "unknown", None, None),
        ("deepseek", "deepseek-flash", None, None),
    ],
)
def test_provider_request_mapping(provider, model, effort, body) -> None:
    settings = AgentSettings(
        _env_file=None, EXTRACT_PROVIDER=provider, EXTRACT_MODEL=model,
        EXTRACT_REASONING_EFFORT=effort, EXTRACT_MAX_TOKENS=1024,
    )
    completions = Completions('{"intents": []}')
    llm = extractor(
        completions, provider=settings.llm_provider, model=settings.extract_model,
        reasoning_effort=settings.extract_reasoning_effort, max_tokens=settings.extract_max_tokens,
    )
    result = llm.complete("SYSTEM", "USER")
    request = completions.requests[0]
    assert request.get("extra_body") == body
    assert ("extra_body" in request) == (body is not None)
    assert request["max_tokens"] == 1024
    assert request["temperature"] == 0
    assert result.usage["reasoning_tokens"] == 0
    assert "PRIVATE REASONING" not in repr(result)


@pytest.mark.parametrize(
    ("provider", "model", "effort"),
    [
        ("tokenhub", "hy3", "disabled"),
        ("tokenhub", "hy3", "NO_THINK"),
        ("tokenhub", "mimo-v2.6-flash", "low"),
        ("tokenhub", "mimo-v2.6-flash", "high"),
        ("tokenhub", "unverified-hy3", "no_think"),
        ("deepseek", "deepseek-flash", "no_think"),
        ("deepseek", "hy3", "low"),
    ],
)
def test_unsupported_settings_rejected_before_model_work(provider, model, effort) -> None:
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, EXTRACT_PROVIDER=provider, EXTRACT_MODEL=model,
                      EXTRACT_REASONING_EFFORT=effort)
    completions = Completions('{"intents": []}')
    with pytest.raises(ValueError):
        extractor(completions, provider=provider, model=model, reasoning_effort=effort)
    assert completions.requests == []


def test_empty_setting_preserves_defaults_and_validator(monkeypatch) -> None:
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "")
    settings = AgentSettings(_env_file=None)
    assert settings.extract_reasoning_effort is None
    assert settings.extract_max_tokens == 4096
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "no_think")
    validator = ValidatorSettings(_env_file=None)
    assert "extract_reasoning_effort" not in validator.model_fields
    assert validator.validate_max_tokens == 4096
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, EXTRACT_MAX_TOKENS=0)


@pytest.mark.parametrize(
    ("content", "finish_reason", "error"),
    [
        (json.dumps(reply(intent("transfer", "50", "Mum", "savings"))),
         "length", "EXTRACTION_TRUNCATED"),
        ('{"intents": [', "length", "EXTRACTION_TRUNCATED"),
        (None, "stop", "EXTRACTION_EMPTY"),
        ("not json", "stop", "EXTRACTION_MALFORMED"),
    ],
)
def test_bad_or_truncated_output_never_proposes(content, finish_reason, error) -> None:
    gw = FakeGateway()
    gw.say("Send 50 to Mum from savings")
    llm = extractor(Completions(content, finish_reason), provider="tokenhub",
                    reasoning_effort="no_think", max_tokens=1024)
    with gw.client() as http:
        result = run_agent(gw.conversation_id, AgentDeps(GatewayClient(http, TOKEN), llm))
    assert result == {"status": "error", "code": error}
    assert not any("/internal/drafts" in str(r.url) for r in gw.requests)


def test_reasoning_and_budget_survive_trace_redaction() -> None:
    tel = Telemetry.for_testing("agent")
    gw = FakeGateway()
    gw.say("Send 50 to Mum from savings")
    llm = extractor(Completions(json.dumps(reply(intent("transfer", "50", "Mum", "savings")))),
                    provider="tokenhub", reasoning_effort="no_think", max_tokens=1024)
    try:
        with gw.client() as http:
            out = run_agent(gw.conversation_id, AgentDeps(GatewayClient(http, TOKEN), llm,
                                                         telemetry=tel))
        assert out["status"] == "draft_frozen"
        tel.flush()
        gen = next(s for s in tel.exporter.get_finished_spans() if s.name == "extractor")
        prefix = "langfuse.observation.metadata."
        assert gen.attributes[prefix + "reasoning_effort"] == "no_think"
        assert gen.attributes[prefix + "max_tokens"] == 1024
        assert gen.attributes[prefix + "provider"] == "tokenhub"
        assert "PRIVATE REASONING" not in str(gen.attributes)
        result = extract_intents(llm, load_extractor_prompt(), [])
        assert result.audit_payload(load_extractor_prompt())["max_tokens"] == 1024
        assert result.audit_payload(load_extractor_prompt())["reasoning_effort"] == "no_think"
    finally:
        tel.shutdown()


@pytest.mark.parametrize("fail", [False, True])
def test_lifespan_reuses_clients_scopes_tokens_and_closes(monkeypatch, fail) -> None:
    settings = AgentSettings(_env_file=None, tokenhub_key="synthetic", EXTRACT_REASONING_EFFORT="")
    models = []
    clients = []
    headers = []

    def make_model(settings):
        model = SimpleNamespace(model="fake", close=lambda: models.append("closed"))
        models.append(model)
        return model

    def handle(request):
        headers.append(request.headers["Authorization"])
        return httpx.Response(200, json=[])

    def make_http(**kwargs):
        client = httpx.Client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    def run(conversation_id, deps, **kwargs):
        assert deps.llm is models[0]
        deps.gateway.list_accounts()
        return {"status": "clarify"}

    monkeypatch.setattr(agent_app, "load_agent_settings", lambda: settings)
    monkeypatch.setattr(OpenAICompatibleExtractor, "from_settings", make_model)
    monkeypatch.setattr(agent_app, "httpx", SimpleNamespace(Client=make_http))
    monkeypatch.setattr(agent_app, "run_agent", run)
    monkeypatch.setattr(agent_app, "_telemetry", None)
    agent_app.app.dependency_overrides[agent_app.get_telemetry] = lambda: None
    try:
        with TestClient(agent_app.app) as client:
            for token in ("owner-one", "owner-two"):
                response = client.post("/internal/run", json={"conversation_id": "c"},
                                       headers={"Authorization": f"Bearer {token}"})
                assert response.json() == {"status": "clarify"}
            assert len(clients) == 1 and not clients[0].is_closed
            assert "Authorization" not in clients[0].headers
            if fail:
                raise RuntimeError("synthetic failure")
    except RuntimeError:
        assert fail
    finally:
        agent_app.app.dependency_overrides.clear()
    assert models[1:] == ["closed"]
    assert clients[0].is_closed
    assert headers == ["Bearer owner-one", "Bearer owner-two"]
