"""Shared helpers for the T-06 policy, execution and idempotency tests.

End-to-end helpers drive the real HTTP routes, the real policy and the real
atomic commit, on top of the T-05 fixtures (software passkey, temp database).
The only test substitutions are module-level and unreachable over HTTP:

- the gateway clock (``gateway.clock._set_clock``), frozen and advanced by hand;
- ``semantic_validation_passed`` (``_set_hook``), which stays fail-closed until T-10;
- the commit hook, swapped for "unavailable" only to record approval evidence
  without executing, so a test can change state between approval and commit.

Policy unit tests build envelopes directly and call ``policy.evaluate``.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gateway.approval.hooks import _reset_hooks, _set_hook
from gateway.bank import BankStore
from gateway.clock import _set_clock
from gateway.execution import commit
from ledger.schema import connect
from shared.money import Money
from shared.schemas import DisplayMetadata, EquityTerms, FrozenBundle, Leg, SigningEnvelope
from shared.timeutil import to_iso
from tests.t05_support import Env, H, b64url_decode, cite, enroll, freeze, new_conversation, say
from tests.webauthn_soft import SoftCredential, create_authentication_response

#: 12:00 Asia/Singapore on a Monday.
T0 = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
USER = "user-matthew"
SAVINGS = "acct-matthew-savings"
CURRENT = "acct-matthew-current"
JOINT = "acct-joint-01"
CARD = "card-matthew-01"


class Clock:
    """A frozen, manually advanced UTC clock."""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now


@dataclass
class T6:
    env: Env
    clock: Clock
    cred: SoftCredential

    @property
    def db(self) -> str:
        return self.env.db_path

    # ── Flow ──────────────────────────────────────────────────────

    def bundle(self, text: str, legs: list[dict[str, Any]]) -> tuple[str, str]:
        """Say *text* in a new conversation and freeze *legs*; return (conv, bundle_id).

        Each leg spec names the words to cite: ``amount``/``amount_words``,
        ``target_id``/``target_words``, ``source_words`` (with optional
        ``*_occ`` occurrence indexes), plus ``source_id``, ``action``,
        ``target_type`` and ``equity_terms``.
        """
        conv = new_conversation(self.env)
        turn = say(self.env, conv, text)
        bodies = []
        for i, spec in enumerate(legs, start=1):
            bodies.append(
                {
                    "leg_id": spec.get("leg_id", f"leg-{i}"),
                    "action": spec.get("action", "transfer"),
                    "source_account_id": spec.get("source_id", SAVINGS),
                    "target_type": spec.get("target_type", "saved_payee"),
                    "target_id": spec["target_id"],
                    "amount_expr": {"kind": "literal", "amount": spec["amount"]},
                    "equity_terms": spec.get("equity_terms"),
                    "evidence": {
                        "amount": cite(
                            turn,
                            spec.get("amount_words", spec["amount"]),
                            spec.get("amount_occ", 0),
                        ),
                        "target": cite(turn, spec["target_words"], spec.get("target_occ", 0)),
                        "source": cite(
                            turn, spec.get("source_words", "savings"), spec.get("source_occ", 0)
                        ),
                    },
                }
            )
        resp = freeze(self.env, conv, bodies)
        assert resp.status_code == 200, resp.text
        return conv, resp.json()["bundle_id"]

    def issue(self, bundle_id: str):
        """POST the authorization request; returns the raw response."""
        return self.env.client.post(
            f"/api/drafts/{bundle_id}/authorizations",
            json={"credential_id": self.cred.credential_id_b64},
            headers=H,
        )

    def issued(self, bundle_id: str) -> dict[str, Any]:
        resp = self.issue(bundle_id)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def assertion(self, view: dict[str, Any], **kw: Any) -> dict[str, Any]:
        # Counter 0 ("not supported") so concurrent assertions cannot trip the
        # library's sign-count ordering check.
        kw.setdefault("sign_count", 0)
        return create_authentication_response(
            self.cred,
            challenge=b64url_decode(view["webauthn_options"]["challenge"]),
            rp_id="localhost",
            origin="http://localhost:5173",
            **kw,
        )

    def approve(self, view: dict[str, Any], **kw: Any):
        """POST a fresh, valid passkey approval; returns the raw response."""
        return self.env.client.post(
            f"/api/authorizations/{view['authorization_id']}/approve",
            json={
                "authorization_id": view["authorization_id"],
                "expected_envelope_hash": view["envelope_hash"],
                "assertion": self.assertion(view, **kw),
            },
            headers=H,
        )

    def pay(self, text: str, legs: list[dict[str, Any]]) -> dict[str, Any]:
        """Freeze, issue and approve; return the approve response body (200 required)."""
        _, bundle_id = self.bundle(text, legs)
        view = self.issued(bundle_id)
        resp = self.approve(view)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def approve_without_commit(self, view: dict[str, Any]) -> None:
        """Record verified approval evidence but leave execution for later."""

        def _unavailable(conn, authz):
            raise NotImplementedError

        _set_hook("commit_authorization", _unavailable)
        try:
            resp = self.approve(view)
            assert (resp.status_code, resp.json()["detail"]) == (503, "COMMIT_NOT_IMPLEMENTED")
        finally:
            _reset_hooks()
            allow_semantic()

    def commit(self, authz_id: str, *, step_up_code: str | None = None, user: str = USER):
        """Run the real commit; ``step_up_code`` submits a T-07 step-up proof."""
        from gateway.stepup import CodeProof

        return commit(
            self.db,
            {"authorization_id": authz_id, "user_id": user},
            self.clock(),
            step_up_proof=CodeProof(step_up_code) if step_up_code is not None else None,
        )

    def step_up_code(self, authz_id: str) -> str:
        """The code the simulated out-of-band channel delivers (operator console service)."""
        from gateway.stepup import StepUpService

        return StepUpService(self.db).operator_deliver(authz_id)["code"]

    # ── Database views ────────────────────────────────────────────

    def q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, params).fetchall()

    def sql(self, sql: str, params: tuple = ()) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(sql, params)

    def balance(self, account_id: str) -> str:
        (cents,) = self.q("SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,))[
            0
        ]
        return f"{cents // 100}.{cents % 100:02d}"

    def balances(self) -> dict[str, int]:
        return dict(self.q("SELECT account_id, balance_cents FROM accounts"))

    def outstanding(self, card_id: str = CARD) -> str:
        (cents,) = self.q("SELECT outstanding_cents FROM cards WHERE card_id = ?", (card_id,))[0]
        return f"{cents // 100}.{cents % 100:02d}"

    def holding(self, symbol: str) -> int:
        rows = self.q(
            "SELECT quantity FROM holdings WHERE owner_user_id = ? AND symbol = ?", (USER, symbol)
        )
        return rows[0][0] if rows else 0

    def count(self, table: str) -> int:
        return self.q(f"SELECT COUNT(*) FROM {table}")[0][0]

    def status(self, authz_id: str) -> str:
        return self.q("SELECT status FROM authorizations WHERE authorization_id = ?", (authz_id,))[
            0
        ][0]

    def draft_status(self, bundle_id: str) -> str:
        return self.q("SELECT status FROM frozen_drafts WHERE bundle_id = ?", (bundle_id,))[0][0]

    def audit_steps(self) -> list[str]:
        return [r[0] for r in self.q("SELECT step FROM audit_events ORDER BY seq")]

    def add_history(
        self,
        amount: str,
        when: datetime,
        *,
        action: str = "pay_bill",
        account_id: str = SAVINGS,
    ) -> None:
        """Insert an earlier committed debit leg (simulated prior activity)."""
        add_history(self.db, amount, when, action=action, account_id=account_id)

    def quote(self, symbol: str = "D05") -> dict[str, Any]:
        return BankStore(self.db).get_quote(symbol, now=self.clock())


def allow_semantic() -> None:
    """Stand in for a passed T-10 semantic validation."""
    _set_hook("semantic_validation_passed", lambda conn, bundle: True)


@pytest.fixture(name="t6")
def t6_fixture(env: Env) -> Iterator[T6]:
    clock = Clock()
    _set_clock(clock)
    allow_semantic()
    cred = enroll(env)
    try:
        yield T6(env=env, clock=clock, cred=cred)
    finally:
        _set_clock(None)
        _reset_hooks()


# ── Direct policy helpers ────────────────────────────────────────


def add_history(
    db: str, amount: str, when: datetime, *, action: str = "pay_bill", account_id: str = SAVINGS
) -> None:
    cents = int(Money(amount).value * 100)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, user_id, account_id, action, target_type, "
            "target_id, amount_cents, status, created_at, direction) "
            "VALUES (?, ?, ?, ?, 'biller', 'biller-spgroup', ?, 'COMMITTED', ?, 'debit')",
            (f"txn-hist-{uuid.uuid4().hex[:8]}", USER, account_id, action, cents, to_iso(when)),
        )


def mk_leg(
    amount: str,
    *,
    target_id: str = "payee-mum",
    target_type: str = "saved_payee",
    action: str = "transfer",
    source: str = SAVINGS,
    leg_id: str | None = None,
    equity_terms: EquityTerms | None = None,
) -> Leg:
    return Leg(
        leg_id=leg_id or f"leg-{uuid.uuid4().hex[:6]}",
        action=action,
        source_account_id=source,
        target_type=target_type,
        target_id=target_id,
        display_metadata=DisplayMetadata(display_name=target_id),
        amount=Money(amount),
        equity_terms=equity_terms,
    )


def mk_envelope(
    legs: list[Leg], now: datetime = T0, *, user_id: str = USER, construct: bool = False
) -> SigningEnvelope:
    """A signing envelope around *legs*; ``construct`` skips schema validation (>4 legs)."""
    fields: dict[str, Any] = {
        "bundle_id": uuid.uuid4(),
        "revision": 1,
        "owner_user_id": user_id,
        "conversation_id": "conv-policy-unit",
        "conversation_revision": 1,
        "conversation_hash": "0" * 64,
        "created_at": to_iso(now),
        "legs": legs,
    }
    bundle = FrozenBundle.model_construct(**fields) if construct else FrozenBundle(**fields)
    env_fields: dict[str, Any] = {
        "schema_version": 1,
        "operation": "execute_bundle",
        "user_id": user_id,
        "credential_id": "cred-unit",
        "operation_id": str(fields["bundle_id"]),
        "payload": bundle,
        "conversation_id": "conv-policy-unit",
        "conversation_revision": 1,
        "conversation_hash": "0" * 64,
        "active_mandate_version": 1,
        "nonce": "ab" * 32,
        "issued_at": to_iso(now),
        "expires_at": to_iso(now + timedelta(seconds=60)),
    }
    if construct:
        return SigningEnvelope.model_construct(**env_fields)
    return SigningEnvelope(**env_fields)


def evaluate(db: str, envelope: SigningEnvelope, now: datetime = T0):
    from gateway.policy import evaluate as _evaluate

    conn = connect(db)
    try:
        return _evaluate(conn, envelope, now, risk_threshold=Money("500.00"))
    finally:
        conn.close()


@pytest.fixture(name="db")
def db_fixture(tmp_path) -> str:
    path = str(tmp_path / "policy.db")
    BankStore(path).init_and_seed()
    return path
