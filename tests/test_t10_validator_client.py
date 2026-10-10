"""T-10 gateway-side validator client: every failure is an ERROR result, never a MATCH.

End-to-end cases run the real validator app (fake model) through Starlette's TestClient;
transport cases use httpx.MockTransport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from shared.canonical import canonical_bytes
from shared.clients.validator import (
    VALIDATE_PATH,
    ValidationRequest,
    ValidatorClient,
    validation_input_hash,
)
from tests.validator_fakes import (
    TOKEN,
    AlwaysMatchLLM,
    ScriptedLLM,
    build_request,
    leg,
    make_settings,
    simple_request,
    turn,
)
from validator.app import create_app
from validator.prompt import PROMPT_HASH

_HASH = "a" * 64


# The client passes its timeout per request; Starlette's TestClient warns about that.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def _e2e(llm: Any, client_token: str = TOKEN, server_token: str | None = TOKEN) -> ValidatorClient:
    app = create_app(settings=make_settings(token=server_token), llm=llm)
    return ValidatorClient("http://testserver", client_token, http_client=TestClient(app))


class _Recorder:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _mock(handler) -> tuple[ValidatorClient, _Recorder]:
    recorder = _Recorder(handler)
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return ValidatorClient("http://validator:8003/", TOKEN, http_client=http), recorder


def _ok_body(request_: ValidationRequest, verdict: str = "MATCH", issues: list | None = None, **kw):
    body = {
        "status": "OK",
        "verdict": verdict,
        "issues": issues or [],
        "model": "deepseek-flash",
        "prompt_hash": _HASH,
        "input_hash": validation_input_hash(request_),
        "latency_ms": 900,
        "usage": None,
    }
    body.update(kw)
    return body


# ── End to end through the real app ─────────────────────────────────


def test_match_round_trip() -> None:
    request_ = simple_request()
    result = _e2e(AlwaysMatchLLM()).validate(request_)
    assert result.status == "OK" and result.verdict == "MATCH" and result.permits_confirmation
    assert result.input_hash == validation_input_hash(request_)
    assert result.prompt_hash == PROMPT_HASH
    assert result.model == "fake-model"


def test_mismatch_round_trip_keeps_issues() -> None:
    issue = {
        "leg_id": "leg-1",
        "field": "amount",
        "reason": "Asked for 50.",
        "supported_value": "50.00 SGD",
        "evidence": [{"turn_id": "turn-1", "start": 5, "end": 7}],
    }
    result = _e2e(ScriptedLLM.verdict("MISMATCH", [issue])).validate(simple_request())
    assert result.verdict == "MISMATCH" and not result.permits_confirmation
    assert [i.model_dump() for i in result.issues] == [issue]


def test_wrong_token_is_unauthorized() -> None:
    result = _e2e(AlwaysMatchLLM(), client_token="z" * 40).validate(simple_request())
    assert result.status == "ERROR" and result.code == "UNAUTHORIZED"


def test_validator_without_token_refuses() -> None:
    result = _e2e(AlwaysMatchLLM(), server_token=None).validate(simple_request())
    assert result.status == "ERROR" and result.code == "SERVICE_TOKEN_UNSET"


def test_model_error_passes_through_as_error() -> None:
    result = _e2e(ScriptedLLM("not json")).validate(simple_request())
    assert result.status == "ERROR" and result.code == "OUTPUT_NOT_JSON"
    assert result.prompt_hash == PROMPT_HASH


# ── Client-side refusals (no network) ────────────────────────────────


@pytest.mark.parametrize("token", [None, "", "short"])
def test_client_without_token_does_not_call(token) -> None:
    client, recorder = _mock(lambda r: httpx.Response(200))
    client = ValidatorClient("http://validator:8003", token, http_client=client._http)
    result = client.validate(simple_request())
    assert result.code == "SERVICE_TOKEN_UNSET"
    assert recorder.requests == []


def test_client_refuses_over_budget_request_locally() -> None:
    turns = [turn(f"turn-{i}", i, "a" * 1999) for i in range(1, 12)]
    request_ = build_request(turns, [leg("leg-1", "transfer", "payee-mum", "50.00")])
    client, recorder = _mock(lambda r: httpx.Response(200))
    assert client.validate(request_).code == "CONTEXT_LIMIT_RESTATE"
    assert recorder.requests == []


def test_request_carries_token_and_canonical_body() -> None:
    request_ = simple_request()
    client, recorder = _mock(lambda r: httpx.Response(200, json=_ok_body(request_)))
    assert client.validate(request_).permits_confirmation
    (sent,) = recorder.requests
    assert sent.method == "POST" and sent.url.path == VALIDATE_PATH
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.content == canonical_bytes(request_)
    assert TOKEN not in repr(client)


# ── Transport and response failures -> ERROR ─────────────────────────


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (_raise(httpx.ReadTimeout("slow")), "VALIDATOR_TIMEOUT"),
        (_raise(httpx.ConnectError("refused")), "VALIDATOR_UNAVAILABLE"),
        (lambda r: httpx.Response(500, text="boom"), "VALIDATOR_HTTP_ERROR"),
        (
            lambda r: httpx.Response(307, headers={"Location": "http://evil.invalid/"}),
            "VALIDATOR_HTTP_ERROR",
        ),
        (
            lambda r: httpx.Response(
                413, json={"status": "ERROR", "code": "CONTEXT_LIMIT_RESTATE"}
            ),
            "CONTEXT_LIMIT_RESTATE",
        ),
        (
            lambda r: httpx.Response(422, json={"status": "ERROR", "code": "MATCH"}),
            "VALIDATOR_HTTP_ERROR",
        ),
        (lambda r: httpx.Response(200, text="MATCH"), "VALIDATOR_RESPONSE_INVALID"),
        (
            lambda r: httpx.Response(200, json={"status": "OK", "verdict": "MATCH"}),
            "VALIDATOR_RESPONSE_INVALID",
        ),
    ],
    ids=[
        "timeout",
        "connect_error",
        "http_500",
        "redirect",
        "413",
        "forged_code",
        "text_body",
        "partial_body",
    ],
)
def test_transport_and_http_failures_are_errors(handler, code) -> None:
    client, _ = _mock(handler)
    result = client.validate(simple_request())
    assert result.status == "ERROR" and result.code == code
    assert not result.permits_confirmation


def test_match_bound_to_a_different_input_is_rejected() -> None:
    request_ = simple_request()
    other = simple_request("Send 5000 dollars to Mum from my savings")
    client, _ = _mock(lambda r: httpx.Response(200, json=_ok_body(other)))
    assert client.validate(request_).code == "INPUT_HASH_MISMATCH"


def _issue(**kw: Any) -> dict:
    base = {
        "leg_id": "leg-1",
        "field": "amount",
        "reason": "r",
        "supported_value": None,
        "evidence": [],
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("verdict", "issues", "extra", "code"),
    [
        ("MATCH", [_issue()], {}, "OUTPUT_VERDICT_INCONSISTENT"),
        ("MISMATCH", [], {}, "OUTPUT_VERDICT_INCONSISTENT"),
        ("MISMATCH", [_issue(leg_id="leg-7")], {}, "OUTPUT_UNKNOWN_LEG"),
        (
            "MISMATCH",
            [_issue(evidence=[{"turn_id": "turn-1", "start": 0, "end": 999}])],
            {},
            "OUTPUT_EVIDENCE_INVALID",
        ),
        ("MATCH", [], {"code": "LLM_TIMEOUT"}, "VALIDATOR_RESPONSE_INVALID"),
        ("MATCH", [], {"debug_force": True}, "VALIDATOR_RESPONSE_INVALID"),
        ("MATCH", [], {"prompt_hash": None}, "VALIDATOR_RESPONSE_INVALID"),
    ],
    ids=[
        "match_with_issues",
        "mismatch_without",
        "unknown_leg",
        "bad_span",
        "ok_with_code",
        "extra_key",
        "no_prompt_hash",
    ],
)
def test_client_rechecks_a_misbehaving_validator(verdict, issues, extra, code) -> None:
    request_ = simple_request()
    body = _ok_body(request_, verdict, issues, **extra)
    client, _ = _mock(lambda r: httpx.Response(200, content=json.dumps(body)))
    result = client.validate(request_)
    assert result.status == "ERROR" and result.code == code
    assert not result.permits_confirmation
