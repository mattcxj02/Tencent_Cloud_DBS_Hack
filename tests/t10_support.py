"""Shared harness for the T-10 gateway validation tests.

Everything runs through the real gateway routes, the real eligibility hook (the
``semantic_validation_passed`` default is NOT overridden), the real policy and commit,
and a software passkey. Test-only substitutions are module-level and unreachable over
HTTP: the gateway clock, the session dependency, the validator client
(``gateway.validation._set_validator_client``: an in-process validator app with a fake
LLM, or an ``httpx.MockTransport``) and the telemetry facade
(``_set_validation_telemetry``: disabled, or ``Telemetry.for_testing``). Settings
objects used here never read ``.env``, so no test exports real traces.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import validation
from gateway.approval.hooks import _reset_hooks
from gateway.clock import _set_clock
from gateway.validation import (
    _set_validation_telemetry,
    _set_validator_client,
    semantic_validation_passed,
)
from ledger.schema import connect
from shared.clients.validator import ValidationRequest, ValidatorClient, validation_input_hash
from shared.schemas import FrozenBundle
from shared.telemetry import Telemetry, httpx_request_hook
from tests.t05_support import Env, H, b64url_decode, cite, enroll, freeze, new_conversation, say
from tests.t06_support import Clock
from tests.validator_fakes import TOKEN, ScriptedLLM, make_settings
from tests.webauthn_soft import SoftCredential, create_authentication_response
from validator.app import create_app as create_validator_app

USER = "user-matthew"
#: Langfuse settings with no keys: Telemetry built from this is disabled (no export).
NO_LANGFUSE = SimpleNamespace(
    langfuse_public_key=None, langfuse_secret_key=None, langfuse_base_url=None
)


def disabled_telemetry(service: str) -> Telemetry:
    return Telemetry(NO_LANGFUSE, service)


def ok_reply(
    request: httpx.Request, verdict: str = "MATCH", issues: list | None = None, **overrides: Any
) -> httpx.Response:
    """A well-formed validator response bound to the request actually sent."""
    parsed = ValidationRequest.model_validate_json(request.content)
    body = {
        "status": "OK",
        "verdict": verdict,
        "issues": issues or [],
        "model": "fake-model",
        "prompt_hash": "a" * 64,
        "input_hash": validation_input_hash(parsed),
        "latency_ms": 7,
        "usage": None,
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


@dataclass
class V:
    env: Env
    clock: Clock
    cred: SoftCredential
    sent: list[httpx.Request] = field(default_factory=list)
    llm: Any = None

    @property
    def db(self) -> str:
        return self.env.db_path

    @property
    def client(self) -> TestClient:
        return self.env.client

    # ── validator wiring ──────────────────────────────────────────

    def use_llm(self, llm: Any, *, telemetry: Telemetry | None = None, **settings: Any) -> None:
        """Route validation to the real validator app (in-process) with a fake model."""
        self.llm = llm
        app = create_validator_app(
            settings=make_settings(**settings),
            llm=llm,
            telemetry=telemetry or disabled_telemetry("validator"),
        )
        http = TestClient(app)
        http.event_hooks = {"request": [httpx_request_hook], "response": []}
        _set_validator_client(ValidatorClient("http://testserver", TOKEN, http_client=http))

    def use_transport(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        """Route validation to a fake transport; requests are recorded in ``sent``."""

        def record(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return handler(request)

        http = httpx.Client(
            transport=httpx.MockTransport(record), event_hooks={"request": [httpx_request_hook]}
        )
        _set_validator_client(ValidatorClient("http://validator.test", TOKEN, http_client=http))

    # ── flow ──────────────────────────────────────────────────────

    def bundle(
        self, text: str, legs: list[dict[str, Any]], conv: str | None = None
    ) -> tuple[str, str]:
        """Say *text* (in a new or given conversation) and freeze *legs*."""
        conv = conv or new_conversation(self.env)
        turn = say(self.env, conv, text)
        bodies = [self.leg_body(turn, spec, i) for i, spec in enumerate(legs, start=1)]
        resp = freeze(self.env, conv, bodies)
        assert resp.status_code == 200, resp.text
        return conv, resp.json()["bundle_id"]

    @staticmethod
    def leg_body(turn: dict[str, Any], spec: dict[str, Any], index: int = 1) -> dict[str, Any]:
        words = spec.get("words", {})
        return {
            "leg_id": spec.get("leg_id", f"leg-{index}"),
            "action": spec.get("action", "transfer"),
            "source_account_id": spec.get("source_id", "acct-matthew-savings"),
            "target_type": spec.get("target_type", "saved_payee"),
            "target_id": spec["target_id"],
            "amount_expr": {"kind": "literal", "amount": spec["amount"]},
            "equity_terms": spec.get("equity_terms"),
            "evidence": {
                "amount": cite(words.get("amount_turn", turn), spec["amount_words"]),
                "target": cite(words.get("target_turn", turn), spec["target_words"]),
                "source": cite(words.get("source_turn", turn), spec.get("source_words", "savings")),
            },
        }

    def mum(self, amount: str = "50.00", words: str = "50") -> tuple[str, str]:
        return self.bundle(
            f"Send {words} to Mum from my savings",
            [{"target_id": "payee-mum", "amount": amount, "amount_words": words,
              "target_words": "Mum"}],
        )  # fmt: skip

    def validate(self, bundle_id: str, **kw: Any) -> httpx.Response:
        kw.setdefault("headers", H)
        return self.client.post(f"/api/drafts/{bundle_id}/validate", **kw)

    def validated(self, bundle_id: str) -> dict[str, Any]:
        resp = self.validate(bundle_id)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def latest(self, bundle_id: str) -> httpx.Response:
        return self.client.get(f"/api/drafts/{bundle_id}/validation")

    def issue(self, bundle_id: str) -> httpx.Response:
        return self.client.post(
            f"/api/drafts/{bundle_id}/authorizations",
            json={"credential_id": self.cred.credential_id_b64},
            headers=H,
        )

    def approve(self, view: dict[str, Any], *, challenge: bytes | None = None) -> httpx.Response:
        assertion = create_authentication_response(
            self.cred,
            challenge=challenge or b64url_decode(view["webauthn_options"]["challenge"]),
            rp_id="localhost",
            origin="http://localhost:5173",
            sign_count=0,
        )
        return self.client.post(
            f"/api/authorizations/{view['authorization_id']}/approve",
            json={
                "authorization_id": view["authorization_id"],
                "expected_envelope_hash": view["envelope_hash"],
                "assertion": assertion,
            },
            headers=H,
        )

    # ── database views ────────────────────────────────────────────

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def sql(self, sql: str, params: tuple = ()) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(sql, params)

    def results(self, bundle_id: str) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM validation_results WHERE bundle_id = ? ORDER BY attempt", (bundle_id,)
        )

    def stored_bundle(self, bundle_id: str) -> FrozenBundle:
        bundle = self.env.drafts.get_draft(bundle_id, USER)
        assert bundle is not None
        return bundle

    def passed(self, bundle_id: str, bundle: FrozenBundle | None = None) -> bool:
        conn = connect(self.db)
        try:
            return semantic_validation_passed(conn, bundle or self.stored_bundle(bundle_id))
        finally:
            conn.close()

    def balance(self, account_id: str = "acct-matthew-savings") -> int:
        return self.q("SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,))[0][
            0
        ]

    def audit_steps(self) -> list[str]:
        return [r["step"] for r in self.q("SELECT step FROM audit_events ORDER BY seq")]


@pytest.fixture(name="v")
def v_fixture(env: Env) -> Iterator[V]:
    clock = Clock()
    _set_clock(clock)
    _set_validation_telemetry(disabled_telemetry("gateway"))
    try:
        cred = enroll(env)
        harness = V(env=env, clock=clock, cred=cred)
        harness.use_llm(ScriptedLLM.verdict("MATCH"))
        yield harness
        assert_test_telemetry(validation._telemetry)
    finally:
        _set_clock(None)
        _set_validator_client(None)
        _set_validation_telemetry(None)
        _reset_hooks()


def assert_test_telemetry(tel: Telemetry | None) -> None:
    """The facade is disabled, or an in-memory one with an unroutable URL; never a
    Langfuse backend configured from ``.env``."""
    assert tel is not None
    if tel._backend is not None:
        assert tel._backend.base_url == "http://langfuse.invalid"
    else:
        assert tel.health()["state"] == "disabled"
