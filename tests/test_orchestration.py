"""``POST /api/conversations/{id}/draft``: the gateway runs agent → validator.

The real agent app (fake extractor LLM) and the real validator app (scripted LLM) run
in-process; the gateway, its delegation issuer, the validation coordinator and the
eligibility hook are the real ones (see ``tests/orch_support.py``).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from gateway.conv_routes import get_conversation_store
from gateway.orchestration import AGENT_TOKEN_TTL_SECONDS
from tests.orch_support import (
    AGENT_SCOPES,
    JOHN,
    JOHN_TEXT,
    MUM,
    MUM_TEXT,
    TWO_LEGS,
    TWO_LEGS_TEXT,
    USER,
    Orch,
    agent_response,
    o_fixture,  # noqa: F401
)
from tests.t05_support import H, say
from tests.t10_support import v_fixture  # noqa: F401
from tests.validator_fakes import ScriptedLLM

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

LEG_KEYS = {
    "leg_id",
    "action",
    "amount",
    "currency",
    "display_name",
    "masked_account_suffix",
    "source_account_id",
    "source_display",
    "reference",
    "equity_terms",
}


def _app_questions(o: Orch, conv: str) -> list[dict[str, Any]]:
    turns = o.client.get(f"/api/conversations/{conv}").json()["turns"]
    return [t for t in turns if t["role"] == "app_question"]


def _drafts(o: Orch, conv: str) -> list[Any]:
    return o.v.q("SELECT bundle_id, status FROM frozen_drafts WHERE conversation_id = ?", (conv,))


# ── clarify / ready / needs_correction ──────────────────────────────


def test_clarify_returns_the_gateway_stored_question(o: Orch) -> None:
    conv = o.conversation(JOHN_TEXT)
    o.extractor(JOHN)
    body = o.drafted(conv)

    [question] = _app_questions(o, conv)
    assert body == {
        "status": "clarify",
        "question": {"turn_id": question["turn_id"], "text": question["text"]},
        "conversation_revision": 1,
    }
    assert "John" in body["question"]["text"]
    assert not _drafts(o, conv)
    assert o.v.llm.calls == []  # nothing to validate


def test_ready_returns_gateway_legs_total_and_validation(o: Orch) -> None:
    conv = o.conversation(TWO_LEGS_TEXT)
    o.extractor(TWO_LEGS)
    body = o.drafted(conv)

    assert body["status"] == "ready"
    assert set(body) == {
        "status",
        "bundle_id",
        "bundle_hash",
        "conversation_revision",
        "legs",
        "total_debit",
        "validation",
    }
    assert body["conversation_revision"] == 1
    assert body["total_debit"] == "692.30"
    mum, card = body["legs"]
    assert all(set(leg) == LEG_KEYS for leg in body["legs"])
    assert mum == {
        "leg_id": "leg-1",
        "action": "transfer",
        "amount": "50.00",
        "currency": "SGD",
        "display_name": "Mum",
        "masked_account_suffix": "9988",
        "source_account_id": "acct-matthew-savings",
        "source_display": "Savings ••1234",
        "reference": None,
        "equity_terms": None,
    }
    assert (card["action"], card["amount"], card["source_display"]) == (
        "pay_card",
        "642.30",
        "Current ••5678",
    )
    stored = o.v.q(
        "SELECT bundle_hash, status FROM frozen_drafts WHERE bundle_id = ?", (body["bundle_id"],)
    )
    assert (stored[0]["bundle_hash"], stored[0]["status"]) == (body["bundle_hash"], "DRAFT_FROZEN")

    validation = body["validation"]
    assert (validation["verdict"], validation["next_action"]) == ("MATCH", "CONFIRM")
    assert validation["eligible_for_confirmation"] is True
    assert validation == o.v.latest(body["bundle_id"]).json()
    assert len(o.v.llm.calls) == 1  # one validator call, made by the gateway
    assert o.v.passed(body["bundle_id"])


def test_mismatch_is_needs_correction_with_issues(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    turn = o.client.get(f"/api/conversations/{conv}").json()["turns"][0]
    start = turn["text"].index("fifty bucks")
    issue = {
        "leg_id": "leg-1",
        "field": "amount",
        "reason": "Disagrees with the customer's words.",
        "supported_value": "50.00",
        "evidence": [{"turn_id": turn["turn_id"], "start": start, "end": start + 11}],
    }
    o.v.use_llm(ScriptedLLM.verdict("MISMATCH", [issue]))
    o.extractor(MUM)
    body = o.drafted(conv)

    assert body["status"] == "needs_correction"
    assert body["validation"]["verdict"] == "MISMATCH"
    assert body["validation"]["issues"] == [issue]
    assert body["validation"]["next_action"] == "CORRECT_DRAFT"
    assert body["legs"][0]["display_name"] == "Mum"
    assert _drafts(o, conv)[0]["status"] == "VALIDATION_MISMATCH"
    assert not o.v.passed(body["bundle_id"])


def test_uncertain_is_needs_correction(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.v.use_llm(ScriptedLLM.verdict("UNCERTAIN"))
    o.extractor(MUM)
    body = o.drafted(conv)
    assert body["status"] == "needs_correction"
    assert (body["validation"]["verdict"], body["validation"]["next_action"]) == (
        "UNCERTAIN",
        "CLARIFY",
    )


# ── errors: stable codes, never a 500 ───────────────────────────────


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _reply(outcome: dict[str, Any]):
    return lambda request: agent_response(request, outcome)


def _raw(status: int, text: str):
    return lambda request: httpx.Response(status, text=text)


@pytest.mark.parametrize(
    ("handler", "http_status", "code"),
    [
        (_raise(httpx.ConnectError("refused")), 503, "AGENT_UNAVAILABLE"),
        (_raise(httpx.ReadTimeout("slow")), 504, "AGENT_TIMEOUT"),
        (_raw(500, "Traceback (most recent call last)"), 502, "AGENT_BAD_RESPONSE"),
        (_raw(200, "not json"), 502, "AGENT_BAD_RESPONSE"),
        (_reply({"status": "approved"}), 502, "AGENT_BAD_RESPONSE"),
        (
            _reply({"status": "error", "code": "EXTRACTOR_UNAVAILABLE"}),
            503,
            "EXTRACTOR_UNAVAILABLE",
        ),
        (_reply({"status": "error", "code": "<b>boom</b>"}), 502, "AGENT_ERROR"),
        (_reply({"status": "draft_frozen", "bundle_id": "not-a-draft"}), 502, "AGENT_BAD_RESPONSE"),
        (
            _reply({"status": "clarify", "question_turn_id": "turn-x", "question": "Northwind?"}),
            502,
            "AGENT_BAD_RESPONSE",
        ),
    ],
)
def test_agent_failures_are_stable_error_codes(
    o: Orch, handler: Any, http_status: int, code: str
) -> None:
    conv = o.conversation(MUM_TEXT)
    o.handler = handler
    resp = o.draft(conv)
    assert (resp.status_code, resp.json()) == (http_status, {"status": "error", "code": code})
    assert "Traceback" not in resp.text
    assert not _drafts(o, conv)


def test_agent_extraction_error_is_reported_not_crashed(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor("this is not json")
    resp = o.draft(conv)
    assert resp.status_code == 502
    assert resp.json()["status"] == "error"
    assert set(resp.json()) == {"status", "code"}
    assert not _drafts(o, conv)


@pytest.mark.parametrize(
    ("exc", "http_status", "code"),
    [
        (httpx.ConnectError("validator down"), 503, "VALIDATOR_UNAVAILABLE"),
        (httpx.ReadTimeout("validator slow"), 504, "VALIDATOR_TIMEOUT"),
    ],
)
def test_validator_unavailable_is_an_error_never_ready(
    o: Orch, exc: Exception, http_status: int, code: str
) -> None:
    o.v.use_transport(_raise(exc))
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    resp = o.draft(conv)
    assert (resp.status_code, resp.json()) == (http_status, {"status": "error", "code": code})
    [draft] = _drafts(o, conv)
    assert not o.v.passed(draft["bundle_id"])


def test_unexpected_failure_is_a_safe_502(o: Orch, monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.orchestration_routes as routes

    def boom(user_id: str, conversation_id: str) -> dict:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(routes, "draft_conversation", boom)
    resp = o.draft(o.conversation(MUM_TEXT))
    assert (resp.status_code, resp.json()) == (502, {"status": "error", "code": "DRAFT_FAILED"})
    assert "secret internal detail" not in resp.text


# ── the delegation token and who may draft ──────────────────────────


def test_agent_token_has_exactly_the_agent_scopes_this_conversation_and_short_ttl(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    assert o.drafted(conv)["status"] == "ready"

    [request] = o.agent_requests
    assert request.url.path == "/internal/run"
    assert request.headers["authorization"].startswith("Bearer dcta_")
    assert "cookie" not in request.headers
    principal = o.env.issuer.verify_token(request.headers["authorization"][len("Bearer ") :])
    assert principal.user_id == USER
    assert principal.conversation_id == conv
    assert set(principal.scopes) == AGENT_SCOPES
    payload = o.last_token_payload()
    ttl = payload["expires_at"] - payload["issued_at"]
    assert ttl == AGENT_TOKEN_TTL_SECONDS <= 300
    assert payload["expires_at"] <= time.time() + 300


def test_another_user_cannot_draft_the_conversation(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.env.as_user("user-ana")
    resp = o.draft(conv)
    assert (resp.status_code, resp.json()) == (
        404,
        {"status": "error", "code": "CONVERSATION_NOT_FOUND"},
    )
    unknown = o.draft("conv-does-not-exist")
    assert unknown.status_code == 404
    assert o.agent_requests == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
def test_origin_is_required(o: Orch, headers: dict[str, str]) -> None:
    conv = o.conversation(MUM_TEXT)
    resp = o.draft(conv, headers=headers)
    assert resp.status_code == 403
    assert o.agent_requests == []


def test_no_session_is_401(o: Orch) -> None:
    from gateway.app import app
    from gateway.auth.session import require_user_session

    conv = o.conversation(MUM_TEXT)
    app.dependency_overrides.pop(require_user_session)
    assert o.draft(conv).status_code == 401
    assert o.agent_requests == []


def test_body_cannot_carry_fields(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    resp = o.draft(conv, json={"verdict": "MATCH"})
    assert resp.status_code == 422
    assert o.agent_requests == []


# ── stale results re-run the agent once ─────────────────────────────


def test_stale_revision_from_agent_reruns_once(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    calls = {"n": 0}

    def stale_then_real(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return agent_response(
                request, {"status": "error", "code": "STALE_CONVERSATION_REVISION"}
            )
        return o.forward(request)

    o.handler = stale_then_real
    assert o.drafted(conv)["status"] == "ready"
    assert len(o.agent_requests) == 2


def test_stale_twice_gives_up(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.handler = lambda r: agent_response(
        r, {"status": "error", "code": "STALE_CONVERSATION_REVISION"}
    )
    resp = o.draft(conv)
    assert resp.json() == {"status": "error", "code": "STALE_CONVERSATION_REVISION"}
    assert len(o.agent_requests) == 2


def test_turn_added_after_freeze_redrafts_at_the_new_revision(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)

    def freeze_then_user_speaks(request: httpx.Request) -> httpx.Response:
        response = o.forward(request)
        if len(o.agent_requests) == 1:  # the user adds a turn before validation starts
            get_conversation_store().append_text_turn(USER, conv, "Yes, from my savings")
        return response

    o.handler = freeze_then_user_speaks
    body = o.drafted(conv)
    assert body["status"] == "ready"
    assert body["conversation_revision"] == 2
    statuses = sorted(r["status"] for r in _drafts(o, conv))
    assert statuses == ["DRAFT_FROZEN", "SUPERSEDED"]
    assert len(o.agent_requests) == 2


class _TurnDuringValidation(ScriptedLLM):
    """MATCH, but the user adds a turn while the first validation call is in flight."""

    def __init__(self, conv: str) -> None:
        super().__init__('{"verdict": "MATCH", "issues": []}')
        self.conv = conv

    def complete(self, messages, *, timeout_seconds):  # noqa: ANN001
        if not self.calls:
            get_conversation_store().append_text_turn(USER, self.conv, "Yes, from my savings")
        return super().complete(messages, timeout_seconds=timeout_seconds)


def test_validation_superseded_mid_call_redrafts_once(o: Orch) -> None:
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    o.v.use_llm(_TurnDuringValidation(conv))
    body = o.drafted(conv)
    assert body["status"] == "ready"
    assert body["conversation_revision"] == 2
    assert len(o.agent_requests) == 2
    states = [r["state"] for r in o.v.q("SELECT state FROM validation_results ORDER BY created_at")]
    assert sorted(states) == ["CURRENT", "SUPERSEDED"]


def test_ready_draft_is_issued_and_new_turn_supersedes_it(o: Orch) -> None:
    """The ready draft is the one the confirmation flow can issue; a later turn
    supersedes it and the next draft request produces a new one."""
    conv = o.conversation(MUM_TEXT)
    o.extractor(MUM)
    first = o.drafted(conv)
    assert o.v.issue(first["bundle_id"]).status_code == 200
    say(o.env, conv, "Actually make it from my current account")
    resp = o.client.post(
        f"/api/drafts/{first['bundle_id']}/authorizations",
        json={"credential_id": o.v.cred.credential_id_b64},
        headers=H,
    )
    assert resp.status_code == 409
