"""Owner/revision-scoped drafting with one deadline and reusable frozen validation input."""

from __future__ import annotations

import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from gateway.auth.delegation import get_delegation_issuer
from gateway.drafting_work import DraftWork, admit, finish
from gateway.readiness import drafting_problem
from gateway.readiness import logger as readiness_logger
from gateway.telemetry import stage
from gateway.validation import (
    ValidationRefused,
    _load_bundle,
    latest_result,
    reusable_draft,
    semantic_validation_passed,
    validate_draft,
)
from ledger.schema import cents_to_money, connect, money_to_cents
from shared.deadline import (
    DEADLINE_HEADER,
    Deadline,
    DeadlineExpired,
    bounded_call,
    current_deadline,
)
from shared.schemas import FrozenBundle, Leg
from shared.settings import load_gateway_settings
from shared.telemetry import inject_trace_headers

__all__ = [
    "AGENT_SCOPES",
    "AGENT_TOKEN_TTL_SECONDS",
    "AGENT_TIMEOUT_SECONDS",
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


_AGENT_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gateway-agent")

_RUN_PATH = "/internal/run"
_ROUTE = "/api/conversations/{conversation_id}/draft"
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_UNAVAILABLE = frozenset(
    {
        "EXTRACTOR_UNAVAILABLE",
        "EXTRACTOR_NOT_CONFIGURED",
        "GATEWAY_UNAVAILABLE",
        "VALIDATOR_UNAVAILABLE",
        "SERVICE_TOKEN_UNSET",
        "VALIDATOR_AUTH_FAILED",
        "SERVICE_CONFIGURATION_INVALID",
        "DRAFTING_UNAVAILABLE",
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


def _call_agent(work: DraftWork, attempt: int) -> dict[str, Any]:
    """One agent run with a fresh, narrowly scoped token. Returns the parsed outcome."""
    token = get_delegation_issuer().issue_token(
        work.user_id,
        list(AGENT_SCOPES),
        conversation_id=work.conversation_id,
        run_id=work.run_id,
        ttl_seconds=AGENT_TOKEN_TTL_SECONDS,
    )
    conversation_id = work.conversation_id
    budget = work.budget.child(_agent_timeout())
    with stage(
        "agent.call",
        attributes={"conversation_id": conversation_id, "attempt": attempt, "route": _RUN_PATH},
    ) as span:
        # traceparent of this span: the agent continues the same trace.
        headers = inject_trace_headers(
            {"Authorization": f"Bearer {token}", DEADLINE_HEADER: budget.header()}
        )
        try:

            def call() -> httpx.Response:
                work.check()
                return get_agent_http().post(
                    _RUN_PATH,
                    json={"conversation_id": conversation_id},
                    headers=headers,
                    timeout=budget.remaining(),
                )

            response = bounded_call(_AGENT_EXECUTOR, budget, call)
            work.check()
        except DeadlineExpired:
            if work.superseded or work.budget.cancelled.is_set():
                raise
            span.set_status("timeout", reason_code="AGENT_TIMEOUT")
            raise OrchestrationError("AGENT_TIMEOUT", 504) from None
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


def _question(
    conn: sqlite3.Connection, user_id: str, conversation_id: str, outcome: dict
) -> dict[str, Any]:
    """The clarifying question as the gateway stored it (role ``app_question``)."""
    turn_id = outcome.get("question_turn_id")
    row = conn.execute(
        "SELECT turn_id, role, text FROM conversation_turns "
        "WHERE turn_id = ? AND conversation_id = ?",
        (turn_id if isinstance(turn_id, str) else "", conversation_id),
    ).fetchone()
    if row is None or row["role"] != "app_question":
        raise OrchestrationError("AGENT_BAD_RESPONSE", 502)
    from gateway.clarifications import public_question, question

    stored = question(conn, conversation_id, row["turn_id"])
    result = {"turn_id": row["turn_id"], "text": row["text"]}
    if stored is not None:
        result["clarification"] = public_question(stored, conn, user_id)
    return result


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
        if validation.get("next_action") == "CONFIRM" and not semantic_validation_passed(
            conn, bundle
        ):
            raise OrchestrationError("VALIDATION_NOT_ELIGIBLE", 503)
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
    """The validation view, or None when this request's draft went stale."""
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
    if view.get("state") == "SUPERSEDED" and "TIMEOUT" in (view.get("code") or ""):
        raise OrchestrationError(view["code"], 504)
    return None if view.get("state") == "SUPERSEDED" else view


def _run(work: DraftWork, *, validation_only: bool = False) -> dict[str, Any]:
    work.check()
    conn = connect(work.db_path)
    try:
        bundle_id = reusable_draft(conn, work.user_id, work.conversation_id, work.revision)
    except ValidationRefused as exc:
        if exc.code == "VALIDATION_FINAL" and isinstance(exc.extra.get("result"), dict):
            return _bundle_response(
                work.db_path, work.user_id, exc.extra["result"]["bundle_id"], exc.extra["result"]
            )
        raise OrchestrationError(exc.code, _http_for(exc.code)) from None
    finally:
        conn.close()
    if bundle_id is None:
        if validation_only:
            raise OrchestrationError("REDRAFT_REQUIRED", 409)
        outcome = _call_agent(work, 1)
        work.check()
        status = outcome["status"]
        if status == "error":
            code = _safe_code(outcome.get("code"), "AGENT_ERROR")
            raise OrchestrationError(code, _http_for(code))
        conn = connect(work.db_path)
        try:
            if status == "clarify":
                return {
                    "status": "clarify",
                    "question": _question(conn, work.user_id, work.conversation_id, outcome),
                    "conversation_revision": work.revision,
                }
            draft, current = _frozen_draft(
                conn, work.user_id, work.conversation_id, outcome.get("bundle_id")
            )
            if not current:
                raise OrchestrationError("CONVERSATION_CHANGED", 503)
            bundle_id = draft["bundle_id"]
        finally:
            conn.close()
    work.check()
    work.progress.retry_validation = True
    work.progress.update("validating")
    budget = work.budget.child(_validator_timeout())
    token = current_deadline.set(budget)
    try:
        view = _validate(work.user_id, bundle_id)
    finally:
        current_deadline.reset(token)
    work.check()
    if view is None:
        raise OrchestrationError("CONVERSATION_CHANGED", 503)
    # Re-read eligibility immediately before publishing instead of trusting the earlier view.
    view = latest_result(work.user_id, bundle_id, db_path=work.db_path)
    work.check()
    return _bundle_response(work.db_path, work.user_id, bundle_id, view)


def _validator_timeout() -> float:
    try:
        return float(load_gateway_settings().validator_client_timeout_seconds)
    except Exception:  # noqa: BLE001 - settings unreadable: assume the default
        return 40.0


def _db_path() -> str:
    from gateway.drafts import get_draft_store

    return get_draft_store().db_path


def draft_conversation(
    user_id: str,
    conversation_id: str,
    *,
    cancelled: threading.Event | None = None,
    budget: Deadline | None = None,
    operation_id: str | None = None,
    expected_revision: int | None = None,
    validation_only: bool = False,
) -> dict[str, Any]:
    """Run agent → validator for the user's conversation and return the UI view.

    Raises :class:`OrchestrationError` (stable code + HTTP status) for anything else.
    The ``gateway.draft`` span (fresh trace, session = conversation) closes before the
    response goes back to the person.
    """
    try:
        budget = budget or Deadline.after(load_gateway_settings().draft_timeout_seconds)
    except Exception:  # noqa: BLE001 - configuration failures never start drafting
        raise OrchestrationError("SERVICE_CONFIGURATION_INVALID", 503) from None
    if cancelled is not None:
        budget.cancelled = cancelled
    try:
        budget.remaining()
    except DeadlineExpired:
        raise OrchestrationError("ORCHESTRATION_TIMEOUT", 504) from None
    db_path = _db_path()
    conn = connect(db_path)
    try:
        revision = _owned_conversation(conn, user_id, conversation_id)  # 404 before tracing
    finally:
        conn.close()
    if expected_revision is not None and expected_revision != revision:
        raise OrchestrationError("STALE_CONVERSATION_REVISION", 409)
    work, leader = admit(
        DraftWork(db_path, user_id, conversation_id, revision, budget), operation_id
    )
    if not leader:
        if validation_only and not work.progress.retry_validation:
            raise OrchestrationError("REDRAFT_REQUIRED", 409)
        try:
            while not work.done.wait(timeout=min(0.05, budget.remaining())):
                budget.remaining()
            if work.error is not None:
                raise work.error
            work.check()
            assert work.result is not None
            if "bundle_id" in work.result and work.result["status"] == "ready":
                view = latest_result(user_id, work.result["bundle_id"], db_path=db_path)
                return _bundle_response(db_path, user_id, work.result["bundle_id"], view)
            return work.result
        except DeadlineExpired:
            raise OrchestrationError("ORCHESTRATION_TIMEOUT", 504) from None
    with stage(
        "gateway.draft",
        user_id=user_id,
        conversation_id=conversation_id,
        attributes={"conversation_id": conversation_id, "route": _ROUTE},
    ) as span:
        try:
            work.check()
            deadline_token = current_deadline.set(work.budget)
            try:
                problem = drafting_problem()
            finally:
                current_deadline.reset(deadline_token)
            work.check()
            if problem is not None:
                readiness_logger.warning(
                    "Drafting unavailable: %s; see README.md setup guidance", problem
                )
                raise OrchestrationError(problem, 503)
            result = _run(work, validation_only=validation_only)
            work.check()
            work.result = result
            work.progress.finish(result=result)
        except DeadlineExpired:
            code = (
                "CONVERSATION_CHANGED"
                if work.superseded
                else "DRAFT_CANCELLED"
                if cancelled is not None and cancelled.is_set()
                else "ORCHESTRATION_TIMEOUT"
            )
            work.error = OrchestrationError(code, 504 if "TIMEOUT" in code else 503)
            work.progress.finish(code=code)
            work.audit_end(code)
            raise work.error from None
        except OrchestrationError as exc:
            work.error = exc
            work.progress.finish(code=exc.code)
            work.audit_end(exc.code)
            span.set_status(exc.telemetry_status, reason_code=exc.code)
            raise
        except Exception as exc:
            work.error = exc
            work.progress.finish(code="DRAFT_FAILED")
            raise
        finally:
            finish(work)
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
