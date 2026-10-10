"""Eligibility hooks for the approval and execution pipeline.

- :func:`semantic_validation_passed` requires a stored MATCH bound to the exact
  bundle and current conversation revision (T-10, ``gateway.validation``); fails closed.
- :func:`policy_precheck` runs the deterministic hard policy
  (:func:`gateway.policy.evaluate`) at the gateway clock's current time.
- :func:`commit_authorization` runs the atomic recheck-and-commit
  (:func:`gateway.execution.commit`).

Routes call these hooks.  Tests override them via :func:`_set_hook` using a
module-level registry that is **not reachable from HTTP** — there is no env
var, header, or query parameter that can activate a test override at runtime.

Overriding ``policy_precheck`` only changes the pre-checks at issuance and
right after the signature.  :func:`gateway.execution.commit` always re-runs
the real policy under the write lock, so no override can make a blocked
payment move money.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PolicyOutcome",
    "Receipt",
    "semantic_validation_passed",
    "policy_precheck",
    "commit_authorization",
    "_set_hook",
    "_reset_hooks",
]


@dataclass(frozen=True)
class PolicyOutcome:
    """Result of the deterministic policy.

    ``decision`` is ``PASS``, ``STEP_UP`` or ``BLOCK``.  ``reason_codes`` are
    stable machine codes (see ``gateway.policy.REASON_CODES``); ``reason`` is
    the same codes comma-joined, kept for callers that want one string.
    """

    decision: str  # "PASS" | "STEP_UP" | "BLOCK"
    reason: str = ""
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Receipt:
    """Persisted receipt returned by :func:`commit_authorization`.

    ``details`` is the stored receipt document (legs, transaction IDs, totals).
    ``replayed`` is True when an idempotent retry returned an existing receipt
    and nothing new was executed.
    """

    authorization_id: str
    receipt_id: str
    status: str
    details: Mapping[str, Any] = field(default_factory=dict)
    replayed: bool = False


# ── Default implementations ────────────────────────────────────────


def semantic_validation_passed(conn: sqlite3.Connection, bundle: Any) -> bool:
    """Return ``True`` only if a stored, CURRENT, OK ``MATCH`` validation result is
    bound to exactly *bundle* (ID, hash, owner, conversation revision and hash, bank
    labels) and that revision is still current. Fails closed on anything else; see
    :func:`gateway.validation.semantic_validation_passed` (T-10).
    """
    from gateway.validation import semantic_validation_passed as _bound_match

    return _bound_match(conn, bundle)


def policy_precheck(conn: sqlite3.Connection, envelope_ctx: Any) -> PolicyOutcome:
    """Run the deterministic hard policy for the envelope at the gateway clock's now."""
    from gateway.clock import utcnow
    from gateway.policy import evaluate

    return evaluate(conn, envelope_ctx, utcnow())


def commit_authorization(conn: sqlite3.Connection, authorization: Any) -> Receipt:
    """Atomically recheck and commit the authorization on *conn* (its own write lock).

    Raises :class:`gateway.execution.ExecutionRefused` when the recheck moves the
    authorization to BLOCKED / AWAITING_STEP_UP / EXPIRED / SUPERSEDED, and
    :class:`gateway.execution.ExecutionFailed` when the transaction rolled back.
    """
    from gateway.clock import utcnow
    from gateway.execution import commit

    return commit(conn, authorization, utcnow())


# ── Test-only override registry (not reachable from HTTP) ──────────

_hooks: dict[str, Callable[..., Any]] = {
    "semantic_validation_passed": semantic_validation_passed,
    "policy_precheck": policy_precheck,
    "commit_authorization": commit_authorization,
}


def _set_hook(name: str, fn: Callable[..., Any]) -> None:
    """Override a hook for testing.  Not reachable from HTTP requests."""
    if name not in _hooks:
        raise KeyError(f"unknown hook: {name}")
    _hooks[name] = fn


def _reset_hooks() -> None:
    """Restore all hooks to their default implementations."""
    _hooks["semantic_validation_passed"] = semantic_validation_passed
    _hooks["policy_precheck"] = policy_precheck
    _hooks["commit_authorization"] = commit_authorization


def _call_semantic_validation_passed(conn: sqlite3.Connection, bundle: Any) -> bool:
    return _hooks["semantic_validation_passed"](conn, bundle)


def _call_policy_precheck(conn: sqlite3.Connection, envelope_ctx: Any) -> PolicyOutcome:
    return _hooks["policy_precheck"](conn, envelope_ctx)


def _call_commit_authorization(conn: sqlite3.Connection, authorization: Any) -> Receipt:
    return _hooks["commit_authorization"](conn, authorization)
