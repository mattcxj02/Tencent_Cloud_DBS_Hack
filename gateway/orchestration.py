"""Draft orchestration: run the agent, then the validator, for one conversation.

``POST /api/conversations/{id}/draft`` (``orchestration_routes.py``) calls
:func:`draft_conversation`. The gateway, not the browser, drives the fixed workflow:

1. The conversation must belong to the session user (else ``CONVERSATION_NOT_FOUND``).
2. The gateway issues a short-lived delegation token (``AGENT_TOKEN_TTL_SECONDS``) bound
   to that user and conversation with exactly :data:`AGENT_SCOPES`, and calls the agent's
   ``POST /internal/run`` with it and the current ``traceparent``.
3. ``clarify``: the question is read back from the gateway's own stored ``app_question``
   turn (the agent's copy of the text is not trusted).
4. ``draft_frozen``: the reported bundle must be the user's ``DRAFT_FROZEN`` draft in this
   conversation; the gateway then runs the semantic validation coordinator
   (:func:`gateway.validation.validate_draft`) directly. The legs and totals in the
   response come from the stored bundle bytes and bank records, never from the agent.
5. A stale result (``STALE_CONVERSATION_REVISION``, a draft superseded by a newer turn, or
   a validation stored ``SUPERSEDED``) re-runs the agent once.

Every failure is a stable code (:class:`OrchestrationError`), mapped by the route to
HTTP 404/502/503/504 with ``{"status": "error", "code": ...}``; nothing here returns a
stack trace. A MATCH only makes the draft eligible for confirmation; signing, ownership
and hard policy still decide. Time is bounded: each agent call by
``AGENT_TIMEOUT_SECONDS``, the validator by its client timeout, the whole request by
two agent runs + one validator call + ``BUDGET_SLACK_SECONDS`` (a retry or a second
validation is refused with ``ORCHESTRATION_TIMEOUT`` when the budget cannot cover it).

This module is imported only by gateway code.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from typing import Any

import httpx

from gateway.auth.delegation import get_delegation_issuer
from gateway.telemetry import stage
from gateway.validation import ValidationRefused, _load_bundle, validate_draft
from ledger.schema import cents_to_money, connect, money_to_cents
from shared.schemas import FrozenBundle, Leg
from shared.settings import load_gateway_settings
from shared.telemetry import inject_trace_headers

__all__ = [
    "AGENT_SCOPES",
    "AGENT_TOKEN_TTL_SECONDS",
    "AGENT_TIMEOUT_SECONDS",
    "BUDGET_SLACK_SECONDS",
    "OrchestrationError",
    "account_display",
    "draft_conversation",
    "get_agent_http",
    "leg_views",
    "total_debit",
]

#: Exactly the scopes the agent needs for one run (read, propose, ask, report).
AGENT_SCOPES: tuple[str, ...] = (
    "conversation:read",
    "bank:read",
    "draft:freeze",
    "conversation:propose_question",
    "audit:append_agent",
)
#: Delegation token lifetime for one agent run (well under the 5-minute default).
AGENT_TOKEN_TTL_SECONDS = 120
#: Default per-call read/write timeout for the agent (it answers with one JSON body);
#: configurable as ``AGENT_TIMEOUT_SECONDS``. 15 s timed out a live hy3 follow-up turn.
AGENT_TIMEOUT_SECONDS = 45.0


def _agent_timeout() -> float:
    try:
        return float(load_gateway_settings().agent_timeout_seconds)
    except Exception:  # noqa: BLE001 - fall back to the default
        return AGENT_TIMEOUT_SECONDS


#: Slack on top of the per-call bounds. One draft request may take at most two agent
#: runs plus one validator call (VALIDATOR_CLIENT_TIMEOUT_SECONDS, default 40 s) plus this.
BUDGET_SLACK_SECONDS = 5.0

_RUN_PATH = "/internal/run"
_ROUTE = "/api/conversations/{conversation_id}/draft"
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_UNAVAILABLE = frozenset(
    {
        "EXTRACTOR_UNAVAILABLE",
        "GATEWAY_UNAVAILABLE",
        "VALIDATOR_UNAVAILABLE",
        "SERVICE_TOKEN_UNSET",
        "LLM_NOT_CONFIGURED",
        "LLM_PROVIDER_ERROR",
        "VALIDATION_RATE_LIMITED",
        "VALIDATION_ATTEMPTS_EXHAUSTED",
        "VALIDATION_IN_PROGRESS",
        "VALIDATION_NOT_ELIGIBLE",
        "CONVERSATION_CHANGED",
    }
)


class OrchestrationError(Exception):
    """A draft request that ends without a clarification or validated draft.

    ``code`` is stable and safe to show; ``http_status`` is 404, 502, 503 or 504.
    """

    def __init__(self, code: str, http_status: int, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason_code = code
        self.http_status = http_status
        self.retry_after = retry_after
        self.telemetry_status = (
            "timeout" if http_status == 504 else "rejected" if http_status == 404 else "error"
        )


def _http_for(code: str) -> int:
    if "TIMEOUT" in code:
        return 504
    return 503 if code in _UNAVAILABLE else 502


def _safe_code(value: Any, default: str) -> str:
    return value if isinstance(value, str) and _CODE_RE.match(value) else default


# ── Agent transport (module-level; tests inject, HTTP cannot) ─────────

_agent_http: httpx.Client | None = None
_http_lock = threading.Lock()


def get_agent_http() -> httpx.Client:
    """The process-wide client for the internal agent service (``AGENT_BASE_URL``)."""
    global _agent_http  # noqa: PLW0603
    with _http_lock:
        if _agent_http is None:
            _agent_http = httpx.Client(
                base_url=load_gateway_settings().agent_base_url,
                timeout=httpx.Timeout(_agent_timeout(), connect=3.0),
                follow_redirects=False,
            )
        return _agent_http


def _set_agent_http(client: httpx.Client | None) -> None:
    """Test hook: an ``httpx.MockTransport`` client or the agent app's ``TestClient``."""
    global _agent_http  # noqa: PLW0603
    with _http_lock:
        _agent_http = client


def _call_agent(user_id: str, conversation_id: str, attempt: int) -> dict[str, Any]:
    """One agent run with a fresh, narrowly scoped token. Returns the parsed outcome."""
    token = get_delegation_issuer().issue_token(
        user_id,
        list(AGENT_SCOPES),
        conversation_id=conversation_id,
        ttl_seconds=AGENT_TOKEN_TTL_SECONDS,
    )
    with stage(
        "agent.call",
        attributes={"conversation_id": conversation_id, "attempt": attempt, "route": _RUN_PATH},
    ) as span:
        # traceparent of this span: the agent continues the same trace.
        headers = inject_trace_headers({"Authorization": f"Bearer {token}"})
        try:
            response = get_agent_http().post(
                _RUN_PATH, json={"conversation_id": conversation_id}, headers=headers
            )
        except httpx.TimeoutException:
            span.set_status("timeout", reason_code="AGENT_TIMEOUT")
            raise OrchestrationError("AGENT_TIMEOUT", 504) from None
        except httpx.HTTPError:
            span.set_status("error", reason_code="AGENT_UNAVAILABLE")
            raise OrchestrationError("AGENT_UNAVAILABLE", 503) from None
        span.set_attributes({"http_status": response.status_code})
        try:
            outcome = response.json() if response.status_code == 200 else None
        except ValueError:
            outcome = None
        if not isinstance(outcome, dict) or outcome.get("status") not in (
            "clarify",
            "draft_frozen",
            "error",
        ):
            span.set_status("error", reason_code="AGENT_BAD_RESPONSE")
            raise OrchestrationError("AGENT_BAD_RESPONSE", 502)
        if outcome["status"] == "clarify":
            span.set_status("clarification", reason_code=_safe_code(outcome.get("reason"), ""))
        elif outcome["status"] == "error":
            span.set_status("error", reason_code=_safe_code(outcome.get("code"), "AGENT_ERROR"))
        else:
            span.set_attributes({"bundle_id": outcome.get("bundle_id")})
        return outcome


# ── Gateway-held views ───────────────────────────────────────────────


def account_display(account_type: str, masked_suffix: str) -> str:
    """``Savings ••1234``: account type and masked suffix from bank records."""
    return f"{str(account_type).title()} ••{masked_suffix}"


def _source_displays(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT a.account_id, a.type, a.masked_suffix FROM accounts a JOIN account_holders h "
        "ON h.account_id = a.account_id WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["account_id"]: account_display(r["type"], r["masked_suffix"]) for r in rows}


def _leg_view(leg: Leg, sources: dict[str, str]) -> dict[str, Any]:
    return {
        "leg_id": leg.leg_id,
        "action": leg.action.value,
        "amount": leg.amount.to_str(),
        "currency": leg.currency,
        "display_name": leg.display_metadata.display_name,
        "masked_account_suffix": leg.display_metadata.masked_account_suffix,
        "source_account_id": leg.source_account_id,
        "source_display": sources.get(leg.source_account_id, "Account"),
        "reference": leg.reference,
        "equity_terms": (
            leg.equity_terms.model_dump(mode="json") if leg.equity_terms is not None else None
        ),
    }


def leg_views(conn: sqlite3.Connection, user_id: str, bundle: FrozenBundle) -> list[dict]:
    """The legs as the UI shows them, from the stored bundle and the user's accounts."""
    sources = _source_displays(conn, user_id)
    return [_leg_view(leg, sources) for leg in bundle.legs]


def total_debit(bundle: FrozenBundle) -> str:
    """Sum of leg debits, computed in integer cents, as a 2-dp string."""
    return cents_to_money(sum(money_to_cents(leg.amount) for leg in bundle.legs)).to_str()


def _owned_conversation(conn: sqlite3.Connection, user_id: str, conversation_id: str) -> int:
    row = conn.execute(
        "SELECT owner_user_id, revision FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None or row["owner_user_id"] != user_id:
        raise OrchestrationError("CONVERSATION_NOT_FOUND", 404)
    return int(row["revision"])


def _question(conn: sqlite3.Connection, conversation_id: str, outcome: dict) -> dict[str, Any]:
    """The clarifying question as the gateway stored it (role ``app_question``)."""
    turn_id = outcome.get("question_turn_id")
    row = conn.execute(
        "SELECT turn_id, role, text FROM conversation_turns "
        "WHERE turn_id = ? AND conversation_id = ?",
        (turn_id if isinstance(turn_id, str) else "", conversation_id),
    ).fetchone()
    if row is None or row["role"] != "app_question":
        raise OrchestrationError("AGENT_BAD_RESPONSE", 502)
    return {"turn_id": row["turn_id"], "text": row["text"]}


def _frozen_draft(
    conn: sqlite3.Connection, user_id: str, conversation_id: str, bundle_id: Any
) -> tuple[sqlite3.Row, bool]:
    """The user's draft the agent reported, and whether it is still current."""
    row = conn.execute(
        "SELECT bundle_id, owner_user_id, conversation_id, conversation_revision, "
        "canonical_bytes, bundle_hash, status FROM frozen_drafts WHERE bundle_id = ?",
        (bundle_id if isinstance(bundle_id, str) else "",),
    ).fetchone()
    if row is None or row["owner_user_id"] != user_id or row["conversation_id"] != conversation_id:
        raise OrchestrationError("AGENT_BAD_RESPONSE", 502)
    current = _owned_conversation(conn, user_id, conversation_id)
    stale = row["status"] == "SUPERSEDED" or int(row["conversation_revision"]) != current
    if not stale and row["status"] != "DRAFT_FROZEN":
        raise OrchestrationError("AGENT_BAD_RESPONSE", 502)
    return row, not stale


def _bundle_response(
    db_path: str, user_id: str, bundle_id: str, validation: dict[str, Any]
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT bundle_id, owner_user_id, conversation_id, conversation_revision, "
            "canonical_bytes, bundle_hash, status FROM frozen_drafts WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        try:
            bundle = _load_bundle(row)
        except ValidationRefused as exc:
            raise OrchestrationError(exc.code, 502) from None
        legs = leg_views(conn, user_id, bundle)
    finally:
        conn.close()
    verdict, state = validation.get("verdict"), validation.get("status")
    if state == "OK" and verdict == "MATCH" and validation.get("next_action") == "CONFIRM":
        status = "ready"
    elif state == "OK" and verdict in ("MISMATCH", "UNCERTAIN"):
        status = "needs_correction"
    elif state == "OK":  # a MATCH no longer bound (bank terms changed meanwhile)
        raise OrchestrationError("VALIDATION_NOT_ELIGIBLE", 503)
    else:
        code = _safe_code(validation.get("code"), "VALIDATION_ERROR")
        raise OrchestrationError(code, _http_for(code))
    return {
        "status": status,
        "bundle_id": str(bundle.bundle_id),
        "bundle_hash": row["bundle_hash"],
        "conversation_revision": bundle.conversation_revision,
        "legs": legs,
        "total_debit": total_debit(bundle),
        "validation": validation,
    }


# ── Orchestration ────────────────────────────────────────────────────


def _validate(user_id: str, bundle_id: str) -> dict[str, Any] | None:
    """The validation view, or None when the draft went stale (redraft once)."""
    try:
        view = validate_draft(user_id, bundle_id)
    except ValidationRefused as exc:
        if exc.code == "VALIDATION_FINAL" and isinstance(exc.extra.get("result"), dict):
            view = exc.extra["result"]
        elif exc.code == "DRAFT_STALE_REVISION" or (
            exc.code == "DRAFT_NOT_ELIGIBLE" and exc.extra.get("draft_status") == "SUPERSEDED"
        ):
            return None
        else:
            raise OrchestrationError(
                exc.code, _http_for(exc.code), retry_after=exc.extra.get("retry_after_seconds")
            ) from None
    return None if view.get("state") == "SUPERSEDED" else view


def _run(user_id: str, conversation_id: str, db_path: str, deadline: float) -> dict[str, Any]:
    for attempt in (1, 2):
        if attempt == 2 and deadline - time.monotonic() < _agent_timeout():
            raise OrchestrationError("ORCHESTRATION_TIMEOUT", 504)
        outcome = _call_agent(user_id, conversation_id, attempt)
        status = outcome["status"]
        if status == "error":
            code = _safe_code(outcome.get("code"), "AGENT_ERROR")
            if code == "STALE_CONVERSATION_REVISION" and attempt == 1:
                continue
            raise OrchestrationError(code, _http_for(code))
        conn = connect(db_path)
        try:
            if status == "clarify":
                revision = _owned_conversation(conn, user_id, conversation_id)
                return {
                    "status": "clarify",
                    "question": _question(conn, conversation_id, outcome),
                    "conversation_revision": revision,
                }
            draft, current = _frozen_draft(conn, user_id, conversation_id, outcome.get("bundle_id"))
        finally:
            conn.close()
        if current:
            # The validator call is bounded by its own client timeout; start it only if
            # the request budget can still cover that.
            if deadline - time.monotonic() < _validator_timeout():
                raise OrchestrationError("ORCHESTRATION_TIMEOUT", 504)
            view = _validate(user_id, draft["bundle_id"])
            if view is not None:
                return _bundle_response(db_path, user_id, draft["bundle_id"], view)
    raise OrchestrationError("CONVERSATION_CHANGED", 503)


def _validator_timeout() -> float:
    try:
        return float(load_gateway_settings().validator_client_timeout_seconds)
    except Exception:  # noqa: BLE001 - settings unreadable: assume the default
        return 40.0


def _db_path() -> str:
    from gateway.drafts import get_draft_store

    return get_draft_store().db_path


def draft_conversation(user_id: str, conversation_id: str) -> dict[str, Any]:
    """Run agent → validator for the user's conversation and return the UI view.

    Raises :class:`OrchestrationError` (stable code + HTTP status) for anything else.
    The ``gateway.draft`` span (fresh trace, session = conversation) closes before the
    response goes back to the person.
    """
    db_path = _db_path()
    conn = connect(db_path)
    try:
        _owned_conversation(conn, user_id, conversation_id)  # 404 before tracing
    finally:
        conn.close()
    budget = 2 * _agent_timeout() + _validator_timeout() + BUDGET_SLACK_SECONDS
    deadline = time.monotonic() + budget
    with stage(
        "gateway.draft",
        user_id=user_id,
        conversation_id=conversation_id,
        attributes={"conversation_id": conversation_id, "route": _ROUTE},
    ) as span:
        try:
            result = _run(user_id, conversation_id, db_path, deadline)
        except OrchestrationError as exc:
            span.set_status(exc.telemetry_status, reason_code=exc.code)
            raise
        span.set_attributes({"conversation_revision": result["conversation_revision"]})
        if result["status"] == "clarify":
            span.set_attributes({"turn_id": result["question"]["turn_id"]})
            span.set_status("clarification", reason_code="QUESTION_ASKED")
            return result
        validation = result["validation"]
        span.set_attributes(
            {
                "bundle_id": result["bundle_id"],
                "bundle_hash": result["bundle_hash"],
                "leg_count": len(result["legs"]),
                "amount": result["total_debit"],
                "currency": "SGD",
                "validator_verdict": validation.get("verdict"),
            }
        )
        if result["status"] == "needs_correction":
            uncertain = validation.get("verdict") == "UNCERTAIN"
            span.set_status(
                "clarification" if uncertain else "rejected",
                reason_code="VALIDATOR_UNCERTAIN" if uncertain else "VALIDATOR_MISMATCH",
            )
        return result
