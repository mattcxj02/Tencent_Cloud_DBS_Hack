"""Phase 4 budgets, stale/cancelled work, single-flight and frozen-draft retries.

Timing assertions allow 200 ms for thread scheduling, ASGI and mandatory local audit.
Providers deliberately ignore timeouts; their eventual output must remain unusable.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from agent.extraction import OpenAICompatibleExtractor
from gateway.orchestration import OrchestrationError, draft_conversation
from shared.deadline import (
    DEADLINE_HEADER,
    Deadline,
    DeadlineExpired,
    bounded_call,
    current_deadline,
)
from shared.settings import GatewaySettings
from tests.agent_support import FakeLLM, intent, reply
from tests.orch_support import MUM, MUM_TEXT, USER, Orch, o_fixture  # noqa: F401
from tests.t05_support import say
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import RaisingLLM, ScriptedLLM, SlowLLM

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
TOLERANCE = 0.2


def configure(monkeypatch, seconds: float) -> None:
    for module in ("gateway.orchestration", "gateway.orchestration_routes"):
        monkeypatch.setattr(
            f"{module}.load_gateway_settings",
            lambda: GatewaySettings(_env_file=None, DRAFT_TIMEOUT_SECONDS=seconds),
        )


class HeldExtractor(FakeLLM):
    def __init__(self, output=MUM):
        super().__init__(output)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def complete(self, system_prompt, user_content):
        self.started.set()
        assert self.release.wait(3)
        result = super().complete(system_prompt, user_content)
        self.finished.set()
        return result


def drafts(o: Orch):
    return o.v.q("SELECT bundle_id FROM frozen_drafts")


@pytest.mark.parametrize(
    "output", [MUM, reply(intent("transfer", "fifty bucks", "John", "savings"))]
)
def test_slow_extractor_is_bounded_and_publishes_no_late_draft_or_question(o, monkeypatch, output):
    configure(monkeypatch, 0.15)
    o.llm = HeldExtractor(output)
    conv = o.conversation(MUM_TEXT)
    started = time.monotonic()
    try:
        response = o.draft(conv)
        assert response.status_code == 504
        assert time.monotonic() - started <= 0.15 + TOLERANCE
        assert o.llm.started.is_set()
    finally:
        o.llm.release.set()
        assert o.llm.finished.wait(2)
    assert not drafts(o)
    assert not o.v.llm.calls
    assert o.v.q("SELECT turn_id FROM conversation_turns WHERE role = 'app_question'") == []
    assert "gateway.draft.ended" in o.v.audit_steps()
    # Even a remote client that ignores the propagated header cannot use its old token.
    headers = {"Authorization": o.agent_requests[0].headers["authorization"]}
    assert o.client.get("/internal/bank/accounts", headers=headers).status_code == 409


def test_agent_honours_expired_header_without_extractor_or_snapshot(o):
    from fastapi.testclient import TestClient

    from agent.app import app, get_extractor, get_gateway_http

    llm = FakeLLM(MUM)
    app.dependency_overrides[get_extractor] = lambda: llm
    app.dependency_overrides[get_gateway_http] = lambda: o.client
    result = TestClient(app).post(
        "/internal/run",
        json={"conversation_id": o.conversation(MUM_TEXT)},
        headers={"Authorization": "Bearer unused", DEADLINE_HEADER: str(time.time() - 1)},
    )
    assert result.json() == {"status": "error", "code": "AGENT_TIMEOUT"}
    assert llm.calls == []


def test_expiry_during_readiness_starts_no_agent_stage(o, monkeypatch):
    configure(monkeypatch, 0.1)
    calls = []

    def delayed_ready():
        calls.append("readiness")
        time.sleep(0.12)
        return None

    monkeypatch.setattr("gateway.orchestration.drafting_problem", delayed_ready)
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.status_code == 504
    assert calls == ["readiness"]
    assert o.agent_requests == []
    assert o.llm.calls == []


def test_expired_executor_queue_never_calls_provider():
    release = threading.Event()
    started = threading.Event()
    calls = []
    with ThreadPoolExecutor(max_workers=1) as executor:

        def occupied():
            started.set()
            release.wait(2)

        first = executor.submit(occupied)
        assert started.wait(1)
        try:
            with pytest.raises(DeadlineExpired):
                bounded_call(executor, Deadline.after(0.04), lambda: calls.append("provider"))
        finally:
            release.set()
            first.result()
    assert calls == []


def test_concurrent_same_owner_revision_extracts_and_validates_once(o):
    conv = o.conversation(MUM_TEXT)
    o.llm = HeldExtractor()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(o.draft, conv)
        assert o.llm.started.wait(1)
        second = executor.submit(o.draft, conv)
        # Keep the provider in flight long enough for the second request to join.
        time.sleep(0.06)
        o.llm.release.set()
        results = [first.result(), second.result()]
    assert all(r.status_code == 200 and r.json()["status"] == "ready" for r in results)
    assert results[0].json()["bundle_id"] == results[1].json()["bundle_id"]
    assert len(o.llm.calls) == len(o.agent_requests) == len(o.v.llm.calls) == 1


def test_cancelled_extraction_cannot_freeze_or_validate(o):
    conv = o.conversation(MUM_TEXT)
    cancelled = threading.Event()
    o.llm = HeldExtractor()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(draft_conversation, USER, conv, cancelled=cancelled)
        assert o.llm.started.wait(1)
        cancelled.set()
        try:
            with pytest.raises(OrchestrationError, match="DRAFT_CANCELLED"):
                future.result(timeout=1)
        finally:
            o.llm.release.set()
            assert o.llm.finished.wait(1)
    assert not drafts(o)
    assert not o.v.llm.calls


def test_http_disconnect_cancels_work(o, monkeypatch):
    from starlette.requests import Request

    async def disconnected(self):
        return o.llm.started.is_set()

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    o.llm = HeldExtractor()
    try:
        response = o.draft(o.conversation(MUM_TEXT))
        assert response.json()["code"] == "DRAFT_CANCELLED"
    finally:
        o.llm.release.set()
        assert o.llm.finished.wait(1)
    assert not drafts(o)
    assert not o.v.llm.calls


def test_overall_deadline_bounds_slow_validation_and_rejects_late_match(o, monkeypatch):
    configure(monkeypatch, 0.15)
    slow = SlowLLM(0.4)
    o.v.use_llm(slow)
    conv = o.conversation(MUM_TEXT)
    started = time.monotonic()
    response = o.draft(conv)
    assert response.status_code == 504
    assert time.monotonic() - started <= 0.15 + TOLERANCE
    [draft] = drafts(o)
    assert not o.v.passed(draft["bundle_id"])
    time.sleep(0.45)  # Let the deliberately uncancellable provider return its MATCH.
    assert not o.v.passed(draft["bundle_id"])
    assert o.v.issue(draft["bundle_id"]).status_code == 409
    [row] = o.v.results(draft["bundle_id"])
    assert row["state"] == "SUPERSEDED"
    assert "gateway.validation.stale" in o.v.audit_steps()


def test_transient_validation_retry_reuses_draft_and_preserves_rate_limit(o):
    conv = o.conversation(MUM_TEXT)
    o.v.use_llm(RaisingLLM(RuntimeError("simulated provider failure")))
    assert o.draft(conv).json()["code"] == "LLM_PROVIDER_ERROR"
    [draft] = drafts(o)
    assert o.draft(conv).json()["code"] == "VALIDATION_RATE_LIMITED"
    o.v.clock.advance(seconds=3)
    o.v.use_llm(ScriptedLLM.verdict("MATCH"))
    result = o.drafted(conv)
    assert result["bundle_id"] == draft["bundle_id"]
    assert result["validation"]["attempt"] == 2
    assert len(o.llm.calls) == len(o.agent_requests) == 1
    again = o.drafted(conv)
    assert again["bundle_id"] == result["bundle_id"]
    assert len(o.v.llm.calls) == 1


def test_retry_attempt_bound_does_not_reset_by_redrafting(o):
    conv = o.conversation(MUM_TEXT)
    o.v.use_llm(RaisingLLM(RuntimeError("simulated provider failure")))
    for _ in range(3):
        assert o.draft(conv).json()["code"] == "LLM_PROVIDER_ERROR"
        o.v.clock.advance(seconds=3)
    assert o.draft(conv).json()["code"] == "VALIDATION_ATTEMPTS_EXHAUSTED"
    assert len(o.llm.calls) == 1
    [draft] = drafts(o)
    assert len(o.v.results(draft["bundle_id"])) == 3


@pytest.mark.parametrize("change", ["edit", "bank", "calculation", "mandate", "quote"])
def test_changed_bindings_prevent_frozen_draft_reuse(o, change):
    text, extraction = MUM_TEXT, MUM
    if change == "calculation":
        text = "Send the rest to Mum from my savings"
        extraction = reply(intent("transfer", "the rest", "Mum", "savings"))
    elif change == "quote":
        text = "Buy $1000 of DBS stock from my savings"
        extraction = reply(intent("buy_equity", "$1000", "DBS stock", "savings"))
    o.extractor(extraction)
    conv = o.conversation(text)
    o.v.use_llm(RaisingLLM(RuntimeError("simulated provider failure")))
    assert o.draft(conv).json()["code"] == "LLM_PROVIDER_ERROR"
    [original] = drafts(o)
    o.v.clock.advance(seconds=3)
    if change == "edit":
        say(o.env, conv, "Actually use my current account")
        o.extractor(reply(intent("transfer", "fifty bucks", "Mum", "current")))
    elif change == "bank":
        o.v.sql("UPDATE payees SET masked_suffix = '9900' WHERE payee_id = 'payee-mum'")
    elif change == "calculation":
        o.v.sql(
            "UPDATE accounts SET balance_cents = balance_cents - 100 "
            "WHERE account_id = 'acct-matthew-savings'"
        )
    elif change == "mandate":
        o.v.sql("UPDATE mandates SET version = version + 1 WHERE user_id = ?", (USER,))
    else:
        o.v.clock.advance(minutes=6)
    o.v.use_llm(ScriptedLLM.verdict("MATCH"))
    result = o.drafted(conv)
    assert result["bundle_id"] != original["bundle_id"]
    assert len(o.agent_requests) == 2
    assert len(drafts(o)) == 2


@pytest.mark.parametrize(
    "code",
    [
        "SERVICE_TOKEN_UNSET",
        "UNAUTHORIZED",
        "LLM_NOT_CONFIGURED",
        "LLM_CONFIGURATION_ERROR",
        "BINDING_MISMATCH",
    ],
)
def test_permanent_validation_error_is_not_retried_or_reextracted(o, code):
    def failed(request: httpx.Request):
        if request.url.path.endswith("/ready"):
            return httpx.Response(200, json={"status": "ready"})
        from shared.clients.validator import ValidationRequest, validation_input_hash

        return httpx.Response(
            200,
            json={
                "status": "ERROR",
                "code": code,
                "input_hash": validation_input_hash(
                    ValidationRequest.model_validate_json(request.content)
                ),
            },
        )

    o.v.use_transport(failed)
    conv = o.conversation(MUM_TEXT)
    assert o.draft(conv).json()["code"] == code
    o.v.clock.advance(seconds=3)
    assert o.draft(conv).json()["code"] == code
    [draft] = drafts(o)
    assert len(o.v.results(draft["bundle_id"])) == 1
    assert o.v.validate(draft["bundle_id"]).json()["detail"] == "VALIDATION_FINAL"
    assert len(o.llm.calls) == 1


def test_semantic_uncertainty_is_not_rerolled(o):
    o.v.use_llm(ScriptedLLM.verdict("UNCERTAIN"))
    conv = o.conversation(MUM_TEXT)
    assert o.drafted(conv)["status"] == "needs_correction"
    assert o.drafted(conv)["status"] == "needs_correction"
    assert len(o.llm.calls) == len(o.v.llm.calls) == 1


def test_provider_receives_remaining_budget_and_sdk_retries_are_disabled(o, monkeypatch):
    configure(monkeypatch, 0.8)
    observed = []

    class SDK:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **options):
            observed.append(options)
            return self

        def create(self, **options):
            observed.append(options)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(MUM)), finish_reason="stop"
                    )
                ],
                usage=None,
            )

    o.llm = OpenAICompatibleExtractor(
        api_key="test",
        base_url="http://unused",
        model="fake",
        max_tokens=4096,
        timeout=40,
        max_retries=2,
        client=SDK(),
    )

    class Validator(ScriptedLLM):
        def complete(self, messages, *, timeout_seconds):
            observed.append({"validator_timeout": timeout_seconds})
            return super().complete(messages, timeout_seconds=timeout_seconds)

    o.v.use_llm(Validator.verdict("MATCH"))
    assert o.drafted(o.conversation(MUM_TEXT))["status"] == "ready"
    assert observed[0] == {"max_retries": 0}
    assert 0 < observed[1]["timeout"] <= 0.8
    assert 0 < observed[2]["validator_timeout"] < observed[1]["timeout"]
    assert DEADLINE_HEADER in o.agent_requests[0].headers


def test_expired_validator_header_never_starts_provider(o):
    from fastapi.testclient import TestClient

    from tests.validator_fakes import AUTH, make_settings, simple_request
    from validator.app import create_app

    llm = ScriptedLLM.verdict("MATCH")
    client = TestClient(create_app(settings=make_settings(), llm=llm))
    response = client.post(
        "/internal/validate",
        json=simple_request().model_dump(mode="json"),
        headers={**AUTH, DEADLINE_HEADER: str(time.time() - 1)},
    )
    assert response.json()["code"] == "LLM_TIMEOUT"
    assert llm.calls == []
    assert current_deadline.get() is None


def test_edit_during_extraction_starts_no_resolution_or_validation(o):
    conv = o.conversation(MUM_TEXT)
    o.llm = HeldExtractor()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(o.draft, conv)
        assert o.llm.started.wait(1)
        say(o.env, conv, "Actually make it from current")
        o.llm.release.set()
        response = future.result(timeout=2)
    assert response.json()["code"] == "CONVERSATION_CHANGED"
    assert not drafts(o)
    assert not o.v.llm.calls
    assert "agent.normalize" not in o.v.audit_steps()


def test_cancellation_during_freeze_rolls_back_draft_and_freeze_audit(o, monkeypatch):
    from gateway import drafting_work
    from gateway import drafts as store_module

    original = store_module.freeze_calculations

    def cancel_after_compile(*args, **kwargs):
        result = original(*args, **kwargs)
        for work in list(drafting_work._runs.values()):
            work.budget.cancelled.set()
        return result

    monkeypatch.setattr(store_module, "freeze_calculations", cancel_after_compile)
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.json()["code"] == "DRAFT_CANCELLED"
    assert not drafts(o)
    assert not o.v.llm.calls
    assert "gateway.draft.freeze" not in o.v.audit_steps()
    assert "gateway.draft.ended" in o.v.audit_steps()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_provider_configuration_errors_are_permanent(o, status):
    import openai

    from validator.llm import OpenAICompatibleLLM

    errors = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        422: openai.UnprocessableEntityError,
    }

    class SDK:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            response = httpx.Response(status, request=httpx.Request("POST", "http://provider.test"))
            raise errors[status](
                "synthetic provider configuration failure", response=response, body={}
            )

    sdk = SDK()
    llm = OpenAICompatibleLLM(
        api_key="test",
        base_url="http://unused",
        model="fake",
        max_tokens=4096,
        client=sdk,
    )
    o.v.use_llm(llm)
    conv = o.conversation(MUM_TEXT)
    assert o.draft(conv).json()["code"] == "LLM_CONFIGURATION_ERROR"
    o.v.clock.advance(seconds=3)
    assert o.draft(conv).json()["code"] == "LLM_CONFIGURATION_ERROR"
    assert sdk.calls == 1
    assert len(o.llm.calls) == 1


def test_validator_stage_timeout_is_reported_as_timeout_and_can_reuse(o, monkeypatch):
    for module in ("gateway.orchestration", "gateway.orchestration_routes"):
        monkeypatch.setattr(
            f"{module}.load_gateway_settings",
            lambda: GatewaySettings(
                _env_file=None,
                DRAFT_TIMEOUT_SECONDS=1,
                VALIDATOR_CLIENT_TIMEOUT_SECONDS=0.08,
            ),
        )
    o.v.use_llm(SlowLLM(0.3))
    conv = o.conversation(MUM_TEXT)
    response = o.draft(conv)
    assert response.status_code == 504
    assert response.json()["code"] == "VALIDATOR_TIMEOUT"
    [draft] = drafts(o)
    assert not o.v.passed(draft["bundle_id"])
    o.v.clock.advance(seconds=3)
    o.v.use_llm(ScriptedLLM.verdict("MATCH"))
    assert o.drafted(conv)["bundle_id"] == draft["bundle_id"]
    assert len(o.llm.calls) == 1
    time.sleep(0.35)  # The earlier late MATCH cannot replace the retry's result.
    assert o.v.latest(draft["bundle_id"]).json()["attempt"] == 2


def test_bank_change_at_response_preparation_cannot_publish_confirmation(o, monkeypatch):
    from gateway import orchestration

    original = orchestration._bundle_response

    def changed(db_path, user_id, bundle_id, validation):
        o.v.sql("UPDATE payees SET masked_suffix = '9901' WHERE payee_id = 'payee-mum'")
        return original(db_path, user_id, bundle_id, validation)

    monkeypatch.setattr(orchestration, "_bundle_response", changed)
    response = o.draft(o.conversation(MUM_TEXT))
    assert response.json()["code"] == "VALIDATION_NOT_ELIGIBLE"
    [draft] = drafts(o)
    assert o.v.issue(draft["bundle_id"]).status_code == 409
