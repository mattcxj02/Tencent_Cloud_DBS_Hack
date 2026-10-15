"""``scripts/operator_console.py``: the simulated phone channel CLI (T-07 demo).

The console runs against the real gateway app (``TestClient``, no cookie jar entries);
``OPERATOR_TOKEN`` is set through the environment like a deployment.
"""

from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient
from scripts import operator_console as console

from gateway.app import app
from tests.t07_support import BIG, BIG_TEXT, OPERATOR_TOKEN, standalone
from tests.t10_support import V, v_fixture  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

ENV = {"OPERATOR_TOKEN": OPERATOR_TOKEN}


def _run(*argv: str, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = console.main(
        list(argv),
        environ=ENV if environ is None else environ,
        client=TestClient(app),
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def _awaiting(v: V) -> str:
    _, bundle_id = v.bundle(BIG_TEXT, [BIG])
    v.validated(bundle_id)
    view = v.issue(bundle_id).json()
    assert v.approve(view).json()["status"] == "AWAITING_STEP_UP"
    return view["authorization_id"]


@pytest.fixture(autouse=True)
def _operator_channel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)


def test_list_then_code_lets_the_user_finish_step_up(v: V) -> None:
    code, out, _ = _run("list")
    assert code == 0 and "No step-up challenges" in out and "SIMULATED" in out

    authz_id = _awaiting(v)
    code, listed, _ = _run("list")
    assert code == 0 and authz_id in listed

    code, out, err = _run("code", authz_id)
    assert (code, err) == (0, "")
    assert "SIMULATED" in out
    match = re.search(rf"code for {authz_id} is (\d{{6}}) ", out)
    assert match is not None, out
    otp = match.group(1)
    assert not standalone(otp, listed)  # listing never shows a code
    submitted = v.client.post(
        f"/api/authorizations/{authz_id}/step-up",
        json={"code": otp},
        headers={"Origin": "http://localhost:5173"},
    )
    assert submitted.json()["status"] == "COMMITTED", submitted.text


@pytest.mark.parametrize("environ", [{}, {"OPERATOR_TOKEN": "too-short"}])
def test_missing_or_short_token_is_a_usage_error(environ: dict[str, str]) -> None:
    code, out, err = _run("list", environ=environ)
    assert code == 2 and out == "" and "OPERATOR_TOKEN" in err


def test_wrong_token_is_refused_and_not_printed(v: V) -> None:
    wrong = "x" * 40
    code, out, err = _run("list", environ={"OPERATOR_TOKEN": wrong})
    assert code == 1 and "HTTP 401" in err
    assert wrong not in out + err


def test_unknown_or_malformed_authorization(v: V) -> None:
    code, _, err = _run("code", "authz-does-not-exist")
    assert code == 1 and "HTTP 404" in err
    code, _, err = _run("code", "../../api/accounts")
    assert code == 1 and "invalid authorization id" in err


def test_client_sends_no_cookies_or_ambient_credentials() -> None:
    client = console.make_client()
    try:
        assert client.base_url.host == "localhost" and client.base_url.port == 8002
        assert len(client.cookies) == 0
        assert "authorization" not in client.headers
        assert client.trust_env is False
    finally:
        client.close()


def test_code_without_an_id_uses_the_only_waiting_payment(v: V) -> None:
    code, out, err = _run("code")
    assert code == 1 and "no step-up code is waiting" in err and "No step-up challenges" in out

    authz_id = _awaiting(v)
    code, out, err = _run("code")
    assert (code, err) == (0, "")
    match = re.search(rf"code for {authz_id} is (\d{{6}}) ", out)
    assert match is not None, out
    submitted = v.client.post(
        f"/api/authorizations/{authz_id}/step-up",
        json={"code": match.group(1)},
        headers={"Origin": "http://localhost:5173"},
    )
    assert submitted.json()["status"] == "COMMITTED", submitted.text


def test_code_without_an_id_refuses_to_pick_between_several(v: V) -> None:
    first, second = _awaiting(v), _awaiting(v)
    code, out, err = _run("code")
    assert code == 1 and "several codes are waiting" in err
    assert first in out and second in out
    assert not re.search(r"\b\d{6}\b", out)  # the list never shows a code
