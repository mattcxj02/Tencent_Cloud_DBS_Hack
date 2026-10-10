"""Shared helpers for the T-07 step-up and mandate tests.

Built on the T-05/T-06 fixtures: real HTTP routes, real policy and commit, a
software passkey, a temporary database and a frozen gateway clock.  The
operator console gets its own ``TestClient`` (no browser cookie jar) and the
``OPERATOR_TOKEN`` is set through the environment, exactly like a deployment.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from tests.t05_support import H
from tests.t06_support import T6

OPERATOR_TOKEN = "operator-test-token-0123456789-abcdefghijkl"
OP = {"X-Operator-Token": OPERATOR_TOKEN}

BIG_TEXT = "Send 600 to Mum from my savings"
BIG = {"amount": "600.00", "amount_words": "600", "target_id": "payee-mum", "target_words": "Mum"}
SMALL_TEXT = "Send 50 to Mum from my savings"
SMALL = {"amount": "50.00", "amount_words": "50", "target_id": "payee-mum", "target_words": "Mum"}


@pytest.fixture(name="operator")
def operator_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A separate operator-console client with OPERATOR_TOKEN configured."""
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    yield TestClient(app)


# ── Step-up flow ─────────────────────────────────────────────────


def awaiting(t6: T6, text: str = BIG_TEXT, leg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze, issue and approve a payment above the risk threshold → AWAITING_STEP_UP."""
    _, bundle_id = t6.bundle(text, [leg or BIG])
    view = t6.issued(bundle_id)
    resp = t6.approve(view)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "AWAITING_STEP_UP", resp.json()
    return view


def deliver(operator: TestClient, authz_id: str) -> dict[str, Any]:
    resp = operator.post(f"/operator/step-up/{authz_id}/deliver", headers=OP)
    assert resp.status_code == 200, resp.text
    return resp.json()


def submit(t6: T6, authz_id: str, code: str, **extra: Any):
    return t6.env.client.post(
        f"/api/authorizations/{authz_id}/step-up", json={"code": code, **extra}, headers=H
    )


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def challenge(t6: T6, authz_id: str) -> dict[str, Any]:
    rows = t6.q(
        "SELECT status, failed_attempts, deliveries, expires_at, status_reason "
        "FROM step_up_challenges WHERE authorization_id = ?",
        (authz_id,),
    )
    assert len(rows) == 1, rows
    status, failed, deliveries, expires_at, reason = rows[0]
    return {
        "status": status,
        "failed": failed,
        "deliveries": deliveries,
        "expires_at": expires_at,
        "reason": reason,
    }


def standalone(code: str, text: str) -> bool:
    """True if *code* occurs in *text* as its own token (not inside a hash or ID)."""
    return re.search(rf"(?<![0-9A-Za-z]){code}(?![0-9A-Za-z])", text) is not None


# ── Mandate flow ─────────────────────────────────────────────────


def request_change(t6: T6, **fields: Any):
    return t6.env.client.post(
        "/api/mandate/changes",
        json={"credential_id": t6.cred.credential_id_b64, **fields},
        headers=H,
    )


def approve_change(t6: T6, view: dict[str, Any], **kw: Any):
    return t6.env.client.post(
        f"/api/mandate/changes/{view['authorization_id']}/approve",
        json={
            "authorization_id": view["authorization_id"],
            "expected_envelope_hash": view["envelope_hash"],
            "assertion": t6.assertion(view, **kw),
        },
        headers=H,
    )


def change(t6: T6, **fields: Any) -> dict[str, Any]:
    """Request and approve a mandate change; return the committed receipt body."""
    resp = request_change(t6, **fields)
    assert resp.status_code == 200, resp.text
    approved = approve_change(t6, resp.json())
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "COMMITTED", body
    return body


def mandate(t6: T6) -> dict[str, Any]:
    resp = t6.env.client.get("/api/mandate")
    assert resp.status_code == 200, resp.text
    return resp.json()
