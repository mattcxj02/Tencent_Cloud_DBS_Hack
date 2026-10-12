"""In-process harness: real gateway routes and stores, real agent graph, real validator app.

Per case, a temporary SQLite database is seeded from ``ledger/seed.py`` and the gateway's
module-level stores point at it (the same test-only wiring the pytest suite uses; none of
it is reachable over HTTP). The conversation turns are created through the gateway's own
routes, the agent runs its fixed graph against the gateway ``TestClient`` with a real
delegation token, and the captured frozen draft is validated by the gateway's validation
coordinator (``gateway.validation.validate_draft``), which builds the request from
gateway-held data and calls the in-process validator app through ``ValidatorClient``.

Deliberately wrong drafts are never frozen by the gateway (its evidence checks would
refuse them). They are built from the captured correct bundle and sent through the same
``ValidatorClient`` with a request made by the gateway's own builders
(``bound_conversation`` + ``build_bank_metadata``).

The deterministic policy probe evaluates ``gateway.policy.evaluate`` on a throwaway
signing envelope for the captured draft. It creates no authorization and moves no money;
its result is reported separately from validator metrics.

This module is evaluation tooling: it imports gateway code (like ``tests/``) and must
never be imported by the agent or validator services.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent.extraction import ExtractorLLM, LLMReply
from agent.gateway_client import GatewayClient
from agent.workflow import AgentDeps, run_agent
from evals.cases import Case, WrongDraft
from gateway.app import app as gateway_app
from gateway.approval.hooks import _reset_hooks
from gateway.auth.delegation import DelegationIssuer, _set_delegation_issuer
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.calculations import bound_calculations
from gateway.clock import utcnow
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore
from gateway.draft_routes import _set_draft_store
from gateway.drafts import DraftStore
from gateway.identity import IdentityStore, _set_identity_store
from gateway.policy import active_mandate_version, evaluate
from gateway.validation import (
    ValidationRefused,
    bound_conversation,
    build_bank_metadata,
    validate_draft,
)
from ledger.schema import connect
from shared.canonical import canonical_bytes
from shared.clients.validator import ValidationRequest, ValidationResult, ValidatorClient
from shared.schemas import FrozenBundle, SigningEnvelope
from shared.telemetry import Telemetry
from shared.timeutil import to_iso

__all__ = [
    "AGENT_SCOPES",
    "USER",
    "CaseEnv",
    "RecordingExtractor",
    "build_wrong_request",
    "case_env",
    "create_turns",
    "policy_probe",
    "run_agent_once",
    "validate_natural",
    "validate_wrong",
]

USER = "user-matthew"
AGENT_SCOPES = [
    "conversation:read",
    "bank:read",
    "draft:freeze",
    "conversation:propose_question",
    "audit:append_agent",
]

_GATEWAY = TestClient(gateway_app)


# ── Per-case gateway environment ──────────────────────────────────────


@dataclass
class CaseEnv:
    case_id: str
    tmp: Path
    db_path: str
    issuer: DelegationIssuer
    drafts: DraftStore
    conversation_id: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def client(self) -> TestClient:
        return _GATEWAY


@contextmanager
def case_env(case_id: str) -> Iterator[CaseEnv]:
    """Seed a fresh temporary database and point the gateway's stores at it while the
    block runs. The database file is kept until :func:`discard` so phase B can validate
    the captured draft later."""
    tmp = Path(tempfile.mkdtemp(prefix=f"dcta_eval_{case_id}_"))
    db_path = str(tmp / "dcta.db")
    bank = BankStore(db_path)
    bank.init_and_seed()
    issuer = DelegationIssuer(secret=secrets.token_urlsafe(48))
    env = CaseEnv(case_id, tmp, db_path, issuer, DraftStore(db_path))
    _set_bank_store(bank)
    _set_conversation_store(ConversationStore(db_path, audio_dir=str(tmp / "audio") + "/"))
    _set_identity_store(IdentityStore(db_path))
    _set_draft_store(env.drafts)
    _set_delegation_issuer(issuer)
    _reset_hooks()
    gateway_app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id=USER)
    try:
        yield env
    finally:
        gateway_app.dependency_overrides.pop(require_user_session, None)
        for reset in (
            _set_bank_store,
            _set_conversation_store,
            _set_identity_store,
            _set_draft_store,
            _set_delegation_issuer,
        ):
            reset(None)  # type: ignore[operator]
        _reset_hooks()


def discard(env: CaseEnv) -> None:
    shutil.rmtree(env.tmp, ignore_errors=True)


def _ok(resp: Any, what: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise RuntimeError(f"{what} failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def create_turns(env: CaseEnv, case: Case) -> list[dict[str, Any]]:
    """Create the case's conversation through the real gateway routes, in order."""
    client = env.client
    conv = _ok(client.post("/api/conversations", json={}), "create conversation")
    conv_id = conv["conversation_id"]
    env.conversation_id = conv_id
    created: list[dict[str, Any]] = []
    last_question: str | None = None
    for turn in case.turns:
        if turn.kind == "text":
            resp = client.post(f"/api/conversations/{conv_id}/text", json={"text": turn.text})
        elif turn.kind == "edit":
            assert turn.edits is not None
            resp = client.post(
                f"/api/conversations/{conv_id}/transcript-edit",
                json={"edits_turn_id": created[turn.edits]["turn_id"], "text": turn.text},
            )
        elif turn.kind == "question":
            # Recorded exactly as the agent would: its own scoped delegation token.
            token = env.issuer.issue_token(
                USER, ["conversation:propose_question"], conversation_id=conv_id
            )
            resp = client.post(
                f"/internal/conversations/{conv_id}/questions",
                json={"text": turn.text},
                headers={"Authorization": f"Bearer {token}"},
            )
        else:
            resp = client.post(
                f"/api/conversations/{conv_id}/answer",
                json={"answers_question_id": last_question, "text": turn.text},
            )
        row = _ok(resp, f"turn {turn.kind}")
        if turn.kind == "question":
            last_question = row["turn_id"]
        created.append(row)
    env.turns = created
    return created


# ── Extractor wrapper (latency, usage and raw answer per call) ─────────


class RecordingExtractor:
    """Wraps any :class:`ExtractorLLM` and records each call for the local results.

    The recorded ``content`` is the model's final JSON answer (quoted words from the
    synthetic conversation); reasoning tokens are never read."""

    def __init__(self, inner: ExtractorLLM) -> None:
        self._inner = inner
        self.model = inner.model
        self.provider = getattr(inner, "provider", None)
        self.reasoning_effort = getattr(inner, "reasoning_effort", None)
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system_prompt: str, user_content: str) -> LLMReply:
        started = time.monotonic()
        try:
            reply = self._inner.complete(system_prompt, user_content)
        except Exception as exc:
            self.calls.append({"latency_ms": _ms(started), "error": type(exc).__name__})
            raise
        self.calls.append(
            {
                "latency_ms": _ms(started),
                "finish_reason": reply.finish_reason,
                "usage": reply.usage,
                "content": reply.content,
            }
        )
        return reply


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ── Agent run and captured draft ──────────────────────────────────────


def bundle_legs(bundle: FrozenBundle) -> list[dict[str, Any]]:
    """The captured legs in the scoring shape (critical + secondary fields)."""
    legs = []
    for leg in bundle.legs:
        terms = leg.equity_terms
        legs.append(
            {
                "leg_id": leg.leg_id,
                "action": leg.action.value,
                "target_type": leg.target_type.value,
                "target_id": leg.target_id,
                "source_account_id": leg.source_account_id,
                "amount": leg.amount.to_str(),
                "reference": leg.reference,
                "budget": terms.budget.to_str() if terms else None,
                "quantity": terms.quantity if terms else None,
                "display_name": leg.display_metadata.display_name,
            }
        )
    return legs


def run_agent_once(
    env: CaseEnv, llm: ExtractorLLM, telemetry: Telemetry | None
) -> tuple[dict[str, Any], int, FrozenBundle | None]:
    """Run the fixed agent graph once on the case's current revision.

    Returns the outcome (with captured legs for a frozen draft), wall-clock latency in
    ms, and the stored frozen bundle."""
    assert env.conversation_id is not None
    token = env.issuer.issue_token(USER, AGENT_SCOPES, conversation_id=env.conversation_id)
    deps = AgentDeps(GatewayClient(env.client, token), llm, telemetry=telemetry)
    started = time.monotonic()
    try:
        outcome = run_agent(env.conversation_id, deps)
    except Exception as exc:  # noqa: BLE001 - recorded as an agent error, like the service
        outcome = {"status": "error", "code": f"AGENT_EXCEPTION_{type(exc).__name__}"}
    latency = _ms(started)
    bundle = None
    if outcome.get("status") == "draft_frozen":
        bundle = env.drafts.get_draft(str(outcome["bundle_id"]), USER)
        if bundle is not None:
            outcome = {**outcome, "legs": bundle_legs(bundle)}
    return outcome, latency, bundle


# ── Deterministic policy probe ────────────────────────────────────────


def policy_probe(db_path: str, bundle: FrozenBundle) -> dict[str, Any]:
    """``gateway.policy.evaluate`` on a throwaway envelope (read-only, nothing stored)."""
    conn = connect(db_path)
    try:
        now = utcnow()
        envelope = SigningEnvelope(
            operation="execute_bundle",
            user_id=bundle.owner_user_id,
            credential_id="eval-policy-probe",
            operation_id=f"eval-{uuid.uuid4().hex[:12]}",
            payload=bundle,
            conversation_id=bundle.conversation_id,
            conversation_revision=bundle.conversation_revision,
            conversation_hash=bundle.conversation_hash,
            active_mandate_version=active_mandate_version(conn, bundle.owner_user_id) or 1,
            nonce=secrets.token_hex(32),
            issued_at=to_iso(now),
            expires_at=to_iso(now + timedelta(seconds=60)),
        )
        outcome = evaluate(conn, envelope, now)
        return {"decision": outcome.decision, "codes": list(outcome.reason_codes)}
    finally:
        conn.close()


# ── Validation of the natural draft (real coordinator) ────────────────


def validate_natural(
    env: CaseEnv,
    bundle_id: str,
    client: ValidatorClient,
    telemetry: Telemetry,
) -> dict[str, Any]:
    """Validate the captured draft through ``gateway.validation.validate_draft``."""
    started = time.monotonic()
    try:
        view = validate_draft(
            USER, bundle_id, client=client, telemetry=telemetry, db_path=env.db_path
        )
    except ValidationRefused as exc:
        return {
            "status": "ERROR",
            "verdict": None,
            "code": f"REFUSED_{exc.code}",
            "issues": [],
            "latency_ms": None,
            "wall_ms": _ms(started),
            "usage": None,
            "trace_id": None,
        }
    usage = None
    conn = sqlite3.connect(env.db_path)
    try:
        row = conn.execute(
            "SELECT usage_json FROM validation_results WHERE result_id = ?", (view["result_id"],)
        ).fetchone()
        if row and row[0]:
            usage = json.loads(row[0])
    finally:
        conn.close()
    return {
        "status": view["status"],
        "verdict": view["verdict"],
        "code": view["code"],
        "issues": view["issues"],
        "latency_ms": view["latency_ms"],
        "wall_ms": _ms(started),
        "usage": usage,
        "trace_id": view["trace_id"],
        "model": view["model"],
        "prompt_hash": view["prompt_hash"],
        "eligible_for_confirmation": view["eligible_for_confirmation"],
    }


# ── Wrong drafts (same client, gateway request builders) ──────────────


class NotConstructible(ValueError):
    """A wrong-draft spec that cannot be applied to this captured bundle."""


def _display_metadata(conn: sqlite3.Connection, target_type: str, target_id: str) -> dict:
    """Display metadata in the gateway's format (gateway/drafts.py), from the case DB."""
    if target_type == "own_account":
        row = conn.execute(
            "SELECT type, masked_suffix FROM accounts WHERE account_id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise NotConstructible(f"unknown account {target_id}")
        return {"display_name": f"{row[0].title()} {row[1]}", "masked_account_suffix": row[1]}
    if target_type in ("saved_payee", "saved_paynow"):
        row = conn.execute(
            "SELECT display_name, masked_suffix FROM payees "
            "WHERE payee_id = ? AND owner_user_id = ?",
            (target_id, USER),
        ).fetchone()
        if row is None:
            raise NotConstructible(f"unknown payee {target_id}")
        return {"display_name": row[0], "masked_account_suffix": row[1]}
    if target_type == "biller":
        row = conn.execute(
            "SELECT display_name FROM billers WHERE biller_id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise NotConstructible(f"unknown biller {target_id}")
        return {"display_name": row[0], "masked_account_suffix": None}
    if target_type == "owned_card":
        row = conn.execute(
            "SELECT masked_suffix FROM cards WHERE card_id = ? AND owner_user_id = ?",
            (target_id, USER),
        ).fetchone()
        if row is None:
            raise NotConstructible(f"unknown card {target_id}")
        return {"display_name": f"Card {row[0]}", "masked_account_suffix": row[0]}
    if target_type == "sgx_symbol":
        return {"display_name": f"SGX: {target_id.upper()}", "masked_account_suffix": None}
    raise NotConstructible(f"unknown target type {target_type}")


def mutate_bundle(conn: sqlite3.Connection, bundle: FrozenBundle, wd: WrongDraft) -> FrozenBundle:
    """Apply one wrong-draft spec to a copy of the captured bundle (new bundle ID)."""
    data = json.loads(canonical_bytes(bundle))
    legs: list[dict[str, Any]] = data["legs"]
    idx = (wd.leg or 1) - 1
    if wd.kind == "wrong_amount":
        if legs[idx].get("equity_terms"):
            raise NotConstructible("amount of an equity leg is fixed by its quote terms")
        legs[idx]["amount"] = wd.amount
    elif wd.kind == "wrong_recipient":
        assert wd.target_type and wd.target_id
        legs[idx]["target_type"] = wd.target_type
        legs[idx]["target_id"] = wd.target_id
        legs[idx]["display_metadata"] = _display_metadata(conn, wd.target_type, wd.target_id)
    elif wd.kind == "wrong_source":
        legs[idx]["source_account_id"] = wd.source_account_id
    elif wd.kind == "missing_leg":
        del legs[idx]
    else:  # extra_leg
        assert wd.new_leg is not None
        used = {leg["leg_id"] for leg in legs}
        leg_id = next(f"leg-{n}" for n in range(len(legs) + 1, 99) if f"leg-{n}" not in used)
        new = wd.new_leg
        legs.append(
            {
                "leg_id": leg_id,
                "action": new.action,
                "source_account_id": new.source_account_id,
                "target_type": new.target_type,
                "target_id": new.target_id,
                "display_metadata": _display_metadata(conn, new.target_type, new.target_id),
                "amount": new.amount,
                "currency": "SGD",
                "reference": new.reference,
            }
        )
    data["bundle_id"] = str(uuid.uuid4())
    try:
        return FrozenBundle.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - schema refuses this variant
        raise NotConstructible(f"schema rejects variant: {type(exc).__name__}") from None


def build_wrong_request(env: CaseEnv, bundle: FrozenBundle, wd: WrongDraft) -> ValidationRequest:
    """The validation request the gateway would build if this wrong bundle were stored."""
    conn = connect(env.db_path)
    try:
        wrong = mutate_bundle(conn, bundle, wd)
        snapshot = bound_conversation(
            conn, bundle.conversation_id, bundle.conversation_revision, bundle.conversation_hash
        )
        metadata = build_bank_metadata(conn, USER, wrong)
        evidence = bound_calculations(conn, bundle)
    finally:
        conn.close()
    return ValidationRequest(
        request_id=f"eval-{env.case_id}-{wd.id}"[:120],
        conversation=snapshot,
        bundle=wrong,
        bank_metadata=metadata,
        calculation_evidence=evidence,
    )


def validate_wrong(
    request: ValidationRequest,
    client: ValidatorClient,
    telemetry: Telemetry,
    *,
    case_id: str,
) -> dict[str, Any]:
    """Validate one wrong draft; the span lets the validator continue this trace."""
    started = time.monotonic()
    with telemetry.workflow_span(
        "eval.wrong_draft_validation",
        session_id=request.conversation.conversation_id,
        attributes={
            "request_id": request.request_id,
            "leg_count": len(request.bundle.legs),
            "synthetic": True,
        },
    ) as span:
        result: ValidationResult = client.validate(request)
        span.set_attributes({"validator_verdict": result.verdict or "ERROR"})
        trace_id = span.trace_id
    return {
        "status": result.status,
        "verdict": result.verdict,
        "code": result.code,
        "issues": [i.model_dump(mode="json") for i in result.issues or []],
        "latency_ms": result.latency_ms,
        "wall_ms": _ms(started),
        "usage": result.usage.model_dump(mode="json") if result.usage else None,
        "trace_id": trace_id,
        "model": result.model,
        "prompt_hash": result.prompt_hash,
        "case_id": case_id,
    }
