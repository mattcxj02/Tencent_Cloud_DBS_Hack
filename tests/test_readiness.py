"""Readiness fails before extraction, authenticates services, and never leaks secrets."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import gateway.readiness as readiness
from agent.app import app as agent_app
from agent.extraction import OpenAICompatibleExtractor
from gateway.orchestration import _set_agent_http
from gateway.stepup import StepUpSettings
from shared.clients.validator import ValidatorClient
from shared.settings import AgentSettings, GatewaySettings, ValidatorSettings
from tests.orch_support import MUM_TEXT, Orch, o_fixture  # noqa: F401
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import AUTH, TOKEN, ScriptedLLM, make_settings
from validator.app import create_app

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


@pytest.mark.parametrize("token", [None, "short-token", "x" * 31])
def test_missing_or_short_token_calls_no_extractor(o: Orch, monkeypatch, token) -> None:
    monkeypatch.setattr(
        readiness,
        "load_gateway_settings",
        lambda: GatewaySettings(_env_file=None, VALIDATOR_SERVICE_TOKEN=token),
    )
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json() == {"status": "error", "code": "SERVICE_TOKEN_UNSET"}
    assert o.agent_requests == []
    assert o.llm.calls == []
    assert o.v.llm.calls == []


def test_mismatched_tokens_call_no_extractor(o: Orch) -> None:
    llm = ScriptedLLM.verdict("MATCH")
    validator = TestClient(create_app(settings=make_settings(token="other-token-" * 4), llm=llm))
    assert validator.get("/health").json()["service_token"] == "configured"
    from gateway.validation import _set_validator_client

    _set_validator_client(ValidatorClient("http://testserver", TOKEN, http_client=validator))
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json()["code"] == "VALIDATOR_AUTH_FAILED"
    assert o.llm.calls == llm.calls == []
    assert o.agent_requests == []


def test_validator_missing_model_calls_no_extractor(o: Orch) -> None:
    from gateway.validation import _set_validator_client

    validator = TestClient(create_app(settings=make_settings()))
    _set_validator_client(ValidatorClient("http://testserver", TOKEN, http_client=validator))
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json()["code"] == "LLM_NOT_CONFIGURED"
    assert o.llm.calls == []


@pytest.mark.parametrize("token", [None, "validator-short-secret"])
def test_validator_unconfigured_token_calls_no_extractor(o: Orch, token, caplog) -> None:
    o.v.use_llm(ScriptedLLM.verdict("MATCH"), token=token)
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_TOKEN_UNSET"
    assert o.llm.calls == o.v.llm.calls == []
    if token is not None:
        assert token not in response.text + caplog.text


@pytest.mark.parametrize("model,key", [("hy3", None), ("", "test-key"), ("hy3", "   ")])
def test_agent_missing_configuration_calls_no_extractor(o: Orch, monkeypatch, model, key) -> None:
    monkeypatch.setattr(
        "agent.app.load_agent_settings",
        lambda: AgentSettings(_env_file=None, EXTRACT_MODEL=model, tokenhub_key=key),
    )
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json()["code"] == "EXTRACTOR_NOT_CONFIGURED"
    assert o.llm.calls == []
    assert o.v.llm.calls == []


def test_stalled_service_check_has_wall_clock_bound(o: Orch, monkeypatch) -> None:
    released = threading.Event()

    def stalled(request):
        released.wait(2)
        return httpx.Response(200, json={"status": "ready"})

    o.v.use_transport(stalled)
    monkeypatch.setattr(readiness, "READINESS_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        response = o.draft(o.conversation(MUM_TEXT))
        assert time.monotonic() - started < 0.5
        assert response.status_code == 503
        assert response.json()["code"] == "READINESS_TIMEOUT"
        assert o.llm.calls == o.agent_requests == []
    finally:
        released.set()


def test_agent_unavailable_calls_no_extractor(o: Orch) -> None:
    def unavailable(request):
        assert request.extensions["timeout"]["read"] == readiness.SERVICE_CHECK_TIMEOUT_SECONDS
        raise httpx.ConnectError("private upstream details")

    _set_agent_http(
        httpx.Client(base_url="http://agent.test", transport=httpx.MockTransport(unavailable))
    )
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 503
    assert response.json()["code"] == "DRAFTING_UNAVAILABLE"
    assert o.llm.calls == []
    assert "private" not in response.text


def test_ready_allows_drafting_without_step_up_secrets(o: Orch, monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "load_step_up_settings",
        lambda: StepUpSettings(_env_file=None),
    )
    response = o.client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["drafting"] == "ready"
    assert response.json()["step_up"] == "unavailable"
    assert response.json()["step_up_persistence"] == "ephemeral"
    assert response.json()["missing_configuration"] == ["OPERATOR_TOKEN", "STEP_UP_SECRET"]
    assert o.llm.calls == o.v.llm.calls == []
    assert o.drafted(o.conversation(MUM_TEXT))["status"] == "ready"
    assert len(o.llm.calls) == len(o.v.llm.calls) == 1


def test_secret_values_never_appear_in_readiness_or_logs(o: Orch, monkeypatch, caplog) -> None:
    secrets = [TOKEN, "extractor-canary-secret", "operator-canary-" * 3, "step-up-canary-" * 3]
    monkeypatch.setattr(
        "agent.app.load_agent_settings",
        lambda: AgentSettings(_env_file=None, tokenhub_key=secrets[1]),
    )
    monkeypatch.setattr(
        readiness,
        "load_step_up_settings",
        lambda: StepUpSettings(
            _env_file=None, OPERATOR_TOKEN=secrets[2], STEP_UP_SECRET=secrets[3]
        ),
    )
    responses = [
        o.client.get("/ready"),
        o.client.get("/api/health"),
        TestClient(agent_app).get("/ready"),
    ]
    llm = ScriptedLLM.verdict("MATCH")
    validator = TestClient(
        create_app(
            settings=ValidatorSettings(
                _env_file=None, VALIDATOR_SERVICE_TOKEN=TOKEN, deepseek_key=secrets[1]
            ),
            llm=llm,
        )
    )
    responses.extend(
        [
            validator.get("/internal/ready", headers=AUTH),
            validator.get("/internal/ready", headers={"Authorization": f"Bearer {secrets[2]}"}),
        ]
    )
    readiness.log_configuration()
    assert llm.calls == []
    for secret in secrets:
        assert all(secret not in response.text for response in responses)
        assert secret not in caplog.text
    assert all(
        response.headers.get("cache-control") == "no-store"
        for response in [responses[0], *responses[2:]]
    )


def test_failure_body_and_exception_never_leak(o: Orch, caplog) -> None:
    secret = "upstream-secret-canary"
    o.v.use_transport(lambda request: httpx.Response(503, json={"code": secret, "headers": secret}))
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.json()["code"] == "VALIDATOR_UNAVAILABLE"
    assert secret not in response.text + caplog.text
    assert o.llm.calls == []


def test_missing_configuration_logs_names_only(o: Orch, monkeypatch, caplog) -> None:
    secret = "short-secret"
    monkeypatch.setattr(
        readiness,
        "load_gateway_settings",
        lambda: GatewaySettings(_env_file=None, VALIDATOR_SERVICE_TOKEN=secret),
    )
    monkeypatch.setattr(
        readiness,
        "load_step_up_settings",
        lambda: StepUpSettings(_env_file=None, OPERATOR_TOKEN=secret, STEP_UP_SECRET=secret),
    )
    readiness.log_configuration()
    assert all(
        name in caplog.text
        for name in ["VALIDATOR_SERVICE_TOKEN", "OPERATOR_TOKEN", "STEP_UP_SECRET"]
    )
    assert secret not in caplog.text
    assert o.client.get("/health").status_code == 200
    assert o.client.get("/ready").status_code == 503


def test_extractor_uses_configured_timeout_and_retry_budget(monkeypatch) -> None:
    captured = {}

    def client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("openai.OpenAI", client)
    settings = AgentSettings(
        _env_file=None,
        tokenhub_key="test-key",
        EXTRACT_TIMEOUT_SECONDS=12,
        EXTRACT_MAX_RETRIES=1,
    )
    OpenAICompatibleExtractor.from_settings(settings)
    assert captured["timeout"] == 12
    assert captured["max_retries"] == 1
    default = AgentSettings(_env_file=None)
    assert default.extract_max_retries == 0
    assert default.extract_timeout_seconds < GatewaySettings(_env_file=None).agent_timeout_seconds


@pytest.mark.parametrize(
    "setting,value",
    [
        ("EXTRACT_TIMEOUT_SECONDS", 0),
        ("EXTRACT_TIMEOUT_SECONDS", 121),
        ("EXTRACT_MAX_RETRIES", -1),
        ("EXTRACT_MAX_RETRIES", 3),
    ],
)
def test_invalid_extractor_budget_rejected(setting, value) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, **{setting: value})
