"""Signed mandate management: ``update_mandate`` envelopes, cooling and pause (T-07).

The user edits their conversational spending mandate (per-leg limit, daily
limit, allowed actions, pause) in a settings form.  The gateway validates the
request **deterministically** (no model is involved for an explicitly edited
form) and issues a :class:`~shared.schemas.SigningEnvelope` with the distinct
operation ``update_mandate``, signed with the same passkey mechanics and the
same ``approval_challenge`` helper as payments.

Payload semantics
-----------------
The signed :class:`~shared.schemas.MandateChange` payload is the **complete
target setting** (both limits, the allowed-action list in canonical order and
the paused flag), computed by the gateway from the current mandate plus the
requested fields.  Fields the user did not change are copied from the current
version, so the user signs exactly the settings that will apply; nothing is
inferred from unsigned request fields at approval time.  ``max_legs`` and
``expires_at`` are not part of ``MandateChange`` and are carried over from the
current version unchanged.

Change rules (prototype choices, not DBS policy)
------------------------------------------------
- **Tightening** (lower limit, removed action, pause) applies immediately as
  a new mandate version, cancels any pending loosening and supersedes the
  user's pending approvals (payments and other mandate changes) and their
  step-up challenges.
- **Loosening** (higher limit, added action, unpause) becomes the user's one
  pending change, effective ``COOLING_HOURS`` (default 12) after approval;
  the old version stays in force meanwhile.  A new loosening replaces
  (cancels) the previous pending one and restarts the cooling clock.
- **Mixed** requests (some fields tighter, some looser) are rejected: submit
  them separately.  A request that changes nothing is rejected.
- Limits must be positive two-decimal SGD amounts; an increase may not exceed
  SGD 20,000.00 (the highest KYC tier ceiling); the per-leg limit may not
  exceed the daily limit when either changes.
- When a pending change's time comes it is activated lazily by
  :func:`activate_due_changes`, called under the write lock by the mandate
  routes, envelope issuance and ``gateway.execution.commit``.  Activation
  creates a new version, so older payment approvals bound to the previous
  version are superseded and need a fresh approval.

Operation-specific policy (:func:`evaluate_change`): ownership and the change
rules only.  No balance, velocity, KYC or semantic check applies, and a
paused or expired mandate can still be managed by its owner (relaxation still
cools).  ``gateway.policy.evaluate`` keeps blocking ``update_mandate`` as an
unsupported *payment* operation, so neither policy can stand in for the other.

**Scope:** pausing stops payments made through this conversational assistant
(simulated).  It does not lock cards, accounts or any other banking channel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from gateway.approval.hooks import PolicyOutcome
from gateway.audit import append_audit
from gateway.clock import utcnow
from gateway.policy import KYC_TIER_DAILY_CENTS, MandateRow, current_mandate
from ledger.schema import cents_to_money, connect, money_to_cents
from shared.canonical import (
    approval_challenge,
    approval_challenge_hex,
    bundle_hash,
    canonical_bytes,
)
from shared.money import Money
from shared.schemas import ENVELOPE_TTL_SECONDS, Action, MandateChange, SigningEnvelope
from shared.timeutil import from_iso, to_iso

__all__ = [
    "OPERATION",
    "SCOPE_NOTE",
    "REASON_CODES",
    "MAX_LIMIT_CENTS",
    "MandateSettings",
    "MandateChangeError",
    "MandateError",
    "MandateService",
    "settings_of",
    "requested_settings",
    "classify",
    "assess_change",
    "evaluate_change",
    "activate_due_changes",
    "supersede_pending_authorizations",
    "cooling_hours",
]

OPERATION = "update_mandate"
ACTION_ORDER: tuple[str, ...] = tuple(action.value for action in Action)
#: Prototype ceiling for any increased limit: the highest KYC tier's daily ceiling.
MAX_LIMIT_CENTS = max(KYC_TIER_DAILY_CENTS.values())
DEFAULT_COOLING_HOURS = 12

SCOPE_NOTE = (
    "Pausing stops payments made through this conversational assistant only (simulated). "
    "It does not lock your cards, accounts or any other banking channel."
)

#: Stable mandate-change codes.  Clients key on these, never on prose.
REASON_CODES: dict[str, str] = {
    "MANDATE_NOT_FOUND": "There is no mandate to manage.",
    "MANDATE_NO_CHANGE": "The request does not change any setting.",
    "MANDATE_CHANGE_MIXED": (
        "The request both tightens and loosens the mandate; submit the decrease and the "
        "increase as separate requests."
    ),
    "MANDATE_LIMIT_INVALID": "Limits must be positive SGD amounts with at most two decimals.",
    "MANDATE_LIMIT_ABOVE_MAXIMUM": "An increased limit cannot exceed SGD 20,000.00.",
    "MANDATE_PER_LEG_ABOVE_DAILY": "The per-leg limit cannot exceed the daily limit.",
    "MANDATE_ACTIONS_DUPLICATE": "Each action may be listed once.",
    "MANDATE_VERSION_CHANGED": "The mandate changed after this request; request it again.",
    "MANDATE_TIGHTENED": "A mandate decrease or pause superseded this pending approval.",
    "MANDATE_ACTIVATED": "A cooled mandate change took effect; approve again.",
    "MANDATE_CHANGE_REPLACED": "A newer pending increase replaced this one.",
    "MANDATE_CHANGE_CANCELLED_BY_TIGHTENING": (
        "A decrease or pause cancelled this pending increase."
    ),
    "UNSUPPORTED_OPERATION": "This is not a mandate change.",
}


class MandateChangeError(ValueError):
    """A requested change breaks the deterministic change rules (stable code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MandateError(Exception):
    """A mandate request or approval was refused.  The message is a stable code."""

    def __init__(self, code: str, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = tuple(reason_codes)


def cooling_hours() -> int:
    """``COOLING_HOURS`` from gateway settings (default 12; below 1 falls back to 12)."""
    from shared.settings import load_gateway_settings

    hours = load_gateway_settings().cooling_hours
    return hours if hours >= 1 else DEFAULT_COOLING_HOURS


# ── Settings arithmetic (pure) ─────────────────────────────────────


def _ordered(actions: Any) -> tuple[str, ...]:
    wanted = {str(getattr(a, "value", a)) for a in actions}
    return tuple(a for a in ACTION_ORDER if a in wanted)


@dataclass(frozen=True)
class MandateSettings:
    """The user-editable mandate settings, in integer cents."""

    per_leg_cents: int
    daily_cents: int
    allowed_actions: tuple[str, ...]
    paused: bool

    def to_payload(self) -> MandateChange:
        return MandateChange(
            per_leg_limit=cents_to_money(self.per_leg_cents),
            daily_limit=cents_to_money(self.daily_cents),
            paused=self.paused,
            allowed_actions=[Action(a) for a in self.allowed_actions],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "per_leg_limit": cents_to_money(self.per_leg_cents).to_str(),
            "daily_limit": cents_to_money(self.daily_cents).to_str(),
            "allowed_actions": list(self.allowed_actions),
            "paused": self.paused,
        }


def settings_of(row: MandateRow) -> MandateSettings:
    return MandateSettings(
        per_leg_cents=row.per_leg_cents,
        daily_cents=row.daily_cents,
        allowed_actions=_ordered(row.allowed_actions),
        paused=row.status == "paused",
    )


def settings_from_payload(payload: MandateChange) -> MandateSettings:
    if payload.per_leg_limit is None or payload.daily_limit is None:
        raise MandateChangeError("MANDATE_LIMIT_INVALID")
    actions = [a.value for a in payload.allowed_actions]
    if len(set(actions)) != len(actions):
        raise MandateChangeError("MANDATE_ACTIONS_DUPLICATE")
    return MandateSettings(
        per_leg_cents=money_to_cents(payload.per_leg_limit),
        daily_cents=money_to_cents(payload.daily_limit),
        allowed_actions=_ordered(actions),
        paused=payload.paused,
    )


def _limit_cents(text: str) -> int:
    try:
        cents = money_to_cents(Money(text))
    except (TypeError, ValueError):
        raise MandateChangeError("MANDATE_LIMIT_INVALID") from None
    if cents <= 0:
        raise MandateChangeError("MANDATE_LIMIT_INVALID")
    return cents


def requested_settings(
    current: MandateSettings,
    *,
    per_leg_limit: str | None = None,
    daily_limit: str | None = None,
    allowed_actions: list[str] | None = None,
    paused: bool | None = None,
) -> MandateSettings:
    """The full target settings for a request (unspecified fields stay as they are)."""
    if per_leg_limit is None and daily_limit is None and allowed_actions is None and paused is None:
        raise MandateChangeError("MANDATE_NO_CHANGE")
    actions = current.allowed_actions
    if allowed_actions is not None:
        values = [str(getattr(a, "value", a)) for a in allowed_actions]
        if len(set(values)) != len(values):
            raise MandateChangeError("MANDATE_ACTIONS_DUPLICATE")
        actions = _ordered(values)
    return MandateSettings(
        per_leg_cents=current.per_leg_cents
        if per_leg_limit is None
        else _limit_cents(per_leg_limit),
        daily_cents=current.daily_cents if daily_limit is None else _limit_cents(daily_limit),
        allowed_actions=actions,
        paused=current.paused if paused is None else paused,
    )


def classify(current: MandateSettings, target: MandateSettings) -> str:
    """``"tighten"`` or ``"loosen"``; mixed or empty changes raise."""
    if target.per_leg_cents <= 0 or target.daily_cents <= 0:
        raise MandateChangeError("MANDATE_LIMIT_INVALID")
    tighten = loosen = False
    for before, after in (
        (current.per_leg_cents, target.per_leg_cents),
        (current.daily_cents, target.daily_cents),
    ):
        if after < before:
            tighten = True
        elif after > before:
            loosen = True
            if after > MAX_LIMIT_CENTS:
                raise MandateChangeError("MANDATE_LIMIT_ABOVE_MAXIMUM")
    if set(current.allowed_actions) - set(target.allowed_actions):
        tighten = True
    if set(target.allowed_actions) - set(current.allowed_actions):
        loosen = True
    if target.paused and not current.paused:
        tighten = True
    if current.paused and not target.paused:
        loosen = True
    if tighten and loosen:
        raise MandateChangeError("MANDATE_CHANGE_MIXED")
    if not (tighten or loosen):
        raise MandateChangeError("MANDATE_NO_CHANGE")
    limits_changed = (target.per_leg_cents, target.daily_cents) != (
        current.per_leg_cents,
        current.daily_cents,
    )
    if limits_changed and target.per_leg_cents > target.daily_cents:
        raise MandateChangeError("MANDATE_PER_LEG_ABOVE_DAILY")
    return "tighten" if tighten else "loosen"


def assess_change(
    current: MandateRow, payload: MandateChange
) -> tuple[str, MandateSettings, MandateSettings]:
    """``(direction, current settings, target settings)`` for a signed payload."""
    before = settings_of(current)
    target = settings_from_payload(payload)
    return classify(before, target), before, target


def evaluate_change(
    conn: sqlite3.Connection, envelope: SigningEnvelope, now: datetime
) -> PolicyOutcome:
    """Operation-specific policy for ``update_mandate`` (ownership + change rules).

    Reads only.  No payment eligibility (balance, velocity, KYC, semantic
    validation) applies, and a paused or expired mandate can be managed.
    """
    del now  # the rules do not depend on time; cooling is applied on commit
    if envelope.operation != OPERATION or not isinstance(envelope.payload, MandateChange):
        return PolicyOutcome("BLOCK", "UNSUPPORTED_OPERATION", ("UNSUPPORTED_OPERATION",))
    current = current_mandate(conn, envelope.user_id)
    if current is None:
        return PolicyOutcome("BLOCK", "MANDATE_NOT_FOUND", ("MANDATE_NOT_FOUND",))
    if current.version != envelope.active_mandate_version:
        return PolicyOutcome("BLOCK", "MANDATE_VERSION_CHANGED", ("MANDATE_VERSION_CHANGED",))
    try:
        assess_change(current, envelope.payload)
    except MandateChangeError as exc:
        return PolicyOutcome("BLOCK", exc.code, (exc.code,))
    return PolicyOutcome("PASS")


# ── Locked state changes (caller holds BEGIN IMMEDIATE) ────────────


def _audit(conn: sqlite3.Connection, user_id: str, step: str, payload: dict[str, Any]) -> None:
    append_audit(conn, source="gateway", user_id=user_id, step=step, payload=payload)


def supersede_pending_authorizations(
    conn: sqlite3.Connection,
    user_id: str,
    reason: str,
    now: datetime,
    *,
    exclude_authorization_id: str | None = None,
) -> list[str]:
    """Supersede every pending approval of *user_id* (and close its step-up challenge)."""
    from gateway.stepup import close_for_authorization_locked

    rows = conn.execute(
        "SELECT authorization_id, status, conversation_id FROM authorizations "
        "WHERE user_id = ? AND status IN ('PENDING_APPROVAL', 'AWAITING_STEP_UP') "
        "AND authorization_id IS NOT ? ORDER BY created_at",
        (user_id, exclude_authorization_id),
    ).fetchall()
    superseded: list[str] = []
    for row in rows:
        cur = conn.execute(
            "UPDATE authorizations SET status = 'SUPERSEDED', status_reason = ? "
            "WHERE authorization_id = ? AND status = ?",
            (reason, row["authorization_id"], row["status"]),
        )
        if not cur.rowcount:
            continue
        close_for_authorization_locked(conn, row["authorization_id"], "SUPERSEDED", (reason,), now)
        append_audit(
            conn,
            source="gateway",
            conversation_id=row["conversation_id"],
            user_id=user_id,
            step="gateway.authorization.superseded",
            payload={
                "authorization_id": row["authorization_id"],
                "from_status": row["status"],
                "reason_codes": [reason],
            },
        )
        superseded.append(row["authorization_id"])
    return superseded


def _cancel_pending(
    conn: sqlite3.Connection, user_id: str, reason: str, now: datetime
) -> list[str]:
    rows = conn.execute(
        "SELECT change_id FROM mandate_changes WHERE user_id = ? AND status = 'PENDING'",
        (user_id,),
    ).fetchall()
    cancelled = []
    for row in rows:
        conn.execute(
            "UPDATE mandate_changes SET status = 'CANCELLED', status_reason = ?, updated_at = ? "
            "WHERE change_id = ? AND status = 'PENDING'",
            (reason, to_iso(now), row["change_id"]),
        )
        _audit(
            conn,
            user_id,
            "gateway.mandate.change_cancelled",
            {"change_id": row["change_id"], "reason_codes": [reason]},
        )
        cancelled.append(row["change_id"])
    return cancelled


def _insert_version(
    conn: sqlite3.Connection,
    user_id: str,
    base: MandateRow,
    target: MandateSettings,
    *,
    effective_at: str,
    now: datetime,
) -> int:
    (highest,) = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM mandates WHERE user_id = ?", (user_id,)
    ).fetchone()
    version = int(highest) + 1
    conn.execute(
        "INSERT INTO mandates (mandate_id, user_id, version, status, per_leg_cents, daily_cents, "
        "allowed_actions, effective_at, created_at, expires_at, max_legs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"mandate-{uuid.uuid4().hex[:16]}",
            user_id,
            version,
            "paused" if target.paused else "active",
            target.per_leg_cents,
            target.daily_cents,
            ",".join(target.allowed_actions),
            effective_at,
            to_iso(now),
            base.expires_at,
            base.max_legs,
        ),
    )
    return version


def activate_due_changes(
    conn: sqlite3.Connection,
    user_id: str,
    now: datetime,
    *,
    exclude_authorization_id: str | None = None,
) -> int | None:
    """Make the user's cooled pending change effective if its time has come.

    Must run inside the caller's write transaction.  Returns the new version,
    or ``None`` when nothing was due.  Pending approvals bound to the old
    version (except *exclude_authorization_id*, which the caller rechecks
    itself) are superseded.
    """
    row = conn.execute(
        "SELECT * FROM mandate_changes WHERE user_id = ? AND status = 'PENDING' "
        "AND effective_at <= ?",
        (user_id, to_iso(now)),
    ).fetchone()
    if row is None:
        return None
    current = current_mandate(conn, user_id)
    if current is None or current.version != int(row["base_version"]):
        # Defensive: any newer version cancels pending changes when it is written.
        conn.execute(
            "UPDATE mandate_changes SET status = 'CANCELLED', status_reason = "
            "'MANDATE_VERSION_CHANGED', updated_at = ? WHERE change_id = ?",
            (to_iso(now), row["change_id"]),
        )
        _audit(
            conn,
            user_id,
            "gateway.mandate.change_cancelled",
            {"change_id": row["change_id"], "reason_codes": ["MANDATE_VERSION_CHANGED"]},
        )
        return None
    target = MandateSettings(
        per_leg_cents=int(row["per_leg_cents"]),
        daily_cents=int(row["daily_cents"]),
        allowed_actions=_ordered(a for a in row["allowed_actions"].split(",") if a),
        paused=bool(row["paused"]),
    )
    version = _insert_version(
        conn, user_id, current, target, effective_at=row["effective_at"], now=now
    )
    conn.execute(
        "UPDATE mandate_changes SET status = 'ACTIVATED', resulting_version = ?, updated_at = ? "
        "WHERE change_id = ?",
        (version, to_iso(now), row["change_id"]),
    )
    superseded = supersede_pending_authorizations(
        conn, user_id, "MANDATE_ACTIVATED", now, exclude_authorization_id=exclude_authorization_id
    )
    _audit(
        conn,
        user_id,
        "gateway.mandate.activated",
        {
            "change_id": row["change_id"],
            "base_version": int(row["base_version"]),
            "version": version,
            "effective_at": row["effective_at"],
            "settings": target.as_dict(),
            "superseded_authorizations": superseded,
        },
    )
    return version


# ── Views ──────────────────────────────────────────────────────────


def _change_row_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "change_id": row["change_id"],
        "authorization_id": row["authorization_id"],
        "direction": row["direction"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "base_version": int(row["base_version"]),
        "resulting_version": row["resulting_version"],
        "per_leg_limit": cents_to_money(int(row["per_leg_cents"])).to_str(),
        "daily_limit": cents_to_money(int(row["daily_cents"])).to_str(),
        "allowed_actions": [a for a in row["allowed_actions"].split(",") if a],
        "paused": bool(row["paused"]),
        "effective_at": row["effective_at"],
        "requested_at": row["created_at"],
    }


def _mandate_view(conn: sqlite3.Connection, user_id: str, now: datetime) -> dict[str, Any]:
    current = current_mandate(conn, user_id)
    if current is None:
        raise MandateError("MANDATE_NOT_FOUND")
    effective_at = conn.execute(
        "SELECT effective_at FROM mandates WHERE user_id = ? AND version = ?",
        (user_id, current.version),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT * FROM mandate_changes WHERE user_id = ? AND status = 'PENDING'", (user_id,)
    ).fetchone()
    history = conn.execute(
        "SELECT * FROM mandate_changes WHERE user_id = ? ORDER BY created_at DESC, rowid DESC "
        "LIMIT 10",
        (user_id,),
    ).fetchall()
    expired = current.expires_at is not None and from_iso(current.expires_at) <= now
    return {
        "simulated": True,
        "current": {
            "version": current.version,
            "status": current.status,
            **settings_of(current).as_dict(),
            "max_legs": current.max_legs,
            "expires_at": current.expires_at,
            "expired": expired,
            "effective_at": effective_at,
        },
        "pending": _change_row_view(pending) if pending is not None else None,
        "cooling_hours": cooling_hours(),
        "scope_note": SCOPE_NOTE,
        "history": [_change_row_view(row) for row in history],
    }


# ── Service (routes call this) ─────────────────────────────────────


class _Stop(Exception):
    """Internal: end the locked approval, committing (persist) or rolling back."""

    def __init__(
        self,
        code: str,
        *,
        persist: bool = False,
        reason_codes: tuple[str, ...] = (),
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.persist = persist
        self.reason_codes = reason_codes
        self.response = response


def _status_response(authz_id: str, status: str, codes: tuple[str, ...]) -> dict[str, Any]:
    return {
        "authorization_id": authz_id,
        "status": status,
        "reason": ",".join(codes),
        "reason_codes": list(codes),
    }


def _receipt_response(authz_id: str, receipt: Any) -> dict[str, Any]:
    return {
        "authorization_id": authz_id,
        "status": receipt.status,
        "receipt_id": receipt.receipt_id,
        "receipt": dict(receipt.details),
        "replayed": receipt.replayed,
    }


def _b64url(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)) if value else b""


class MandateService:
    """View, request and approve mandate changes for the authenticated owner."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return connect(self._db_path)

    def _drafts(self) -> Any:
        from gateway.drafts import DraftStore

        return DraftStore(self._db_path)

    # ── View ──────────────────────────────────────────────────────

    def view(self, user_id: str) -> dict[str, Any]:
        """Current settings, any pending change with its activation time, recent history."""
        now = utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            activate_due_changes(conn, user_id, now)
            view = _mandate_view(conn, user_id, now)
            conn.commit()
            return view
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    # ── Request (issue the update_mandate envelope) ──────────────

    def request_change(
        self,
        *,
        user_id: str,
        credential_id: str,
        per_leg_limit: str | None = None,
        daily_limit: str | None = None,
        allowed_actions: list[str] | None = None,
        paused: bool | None = None,
    ) -> str:
        """Validate the requested change and issue its envelope; return the authorization ID."""
        from gateway.identity import get_identity_store

        cred = get_identity_store().get_credential(credential_id)
        if cred is None or cred["user_id"] != user_id:
            raise MandateError("CREDENTIAL_NOT_OWNED")

        now = utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            activate_due_changes(conn, user_id, now)
            current = current_mandate(conn, user_id)
            if current is None:
                raise MandateError("MANDATE_NOT_FOUND")
            before = settings_of(current)
            try:
                target = requested_settings(
                    before,
                    per_leg_limit=per_leg_limit,
                    daily_limit=daily_limit,
                    allowed_actions=allowed_actions,
                    paused=paused,
                )
                direction = classify(before, target)
            except MandateChangeError as exc:
                raise MandateError(exc.code, (exc.code,)) from None

            change_id = f"mchg-{uuid.uuid4().hex}"
            envelope = SigningEnvelope(
                schema_version=1,
                operation=OPERATION,
                user_id=user_id,
                credential_id=credential_id,
                operation_id=change_id,
                payload=target.to_payload(),
                active_mandate_version=current.version,
                nonce=secrets.token_hex(32),
                issued_at=to_iso(now),
                expires_at=to_iso(now + timedelta(seconds=ENVELOPE_TTL_SECONDS)),
            )
            policy = evaluate_change(conn, envelope, now)
            if policy.decision != "PASS":
                raise MandateError(policy.reason_codes[0], policy.reason_codes)

            authz_id = f"authz-{uuid.uuid4().hex[:16]}"
            env_hash = approval_challenge_hex(envelope)
            conn.execute(
                "INSERT INTO authorizations (authorization_id, bundle_id, user_id, credential_id, "
                "operation, envelope_bytes, envelope_hash, active_mandate_version, "
                "conversation_id, conversation_revision, status, issued_at, expires_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'PENDING_APPROVAL', "
                "?, ?, ?)",
                (
                    authz_id,
                    change_id,  # the operation ID column (named bundle_id since T-05)
                    user_id,
                    credential_id,
                    OPERATION,
                    canonical_bytes(envelope),
                    env_hash,
                    current.version,
                    envelope.issued_at,
                    envelope.expires_at,
                    to_iso(now),
                ),
            )
            _audit(
                conn,
                user_id,
                "gateway.mandate.change_requested",
                {
                    "authorization_id": authz_id,
                    "change_id": change_id,
                    "envelope_hash": env_hash,
                    "operation": OPERATION,
                    "direction": direction,
                    "base_version": current.version,
                    "settings": target.as_dict(),
                },
            )
            conn.commit()
            return authz_id
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def change_view(self, user_id: str, authz_id: str) -> dict[str, Any]:
        """The stored envelope, WebAuthn options and a plain summary of the change."""
        drafts = self._drafts()
        authz = drafts.get_authorization(authz_id, user_id)
        if authz is None or authz["operation"] != OPERATION:
            raise MandateError("AUTHORIZATION_NOT_FOUND")
        envelope = SigningEnvelope.model_validate(json.loads(authz["envelope_bytes"]))
        conn = self._conn()
        try:
            base = conn.execute(
                "SELECT version, status, per_leg_cents, daily_cents, allowed_actions, max_legs, "
                "expires_at FROM mandates WHERE user_id = ? AND version = ?",
                (user_id, authz["active_mandate_version"]),
            ).fetchone()
        finally:
            conn.close()
        summary: dict[str, Any] = {"requested": None, "direction": None}
        if base is not None and isinstance(envelope.payload, MandateChange):
            base_row = MandateRow(
                version=int(base[0]),
                status=base[1],
                per_leg_cents=int(base[2]),
                daily_cents=int(base[3]),
                allowed_actions=frozenset(a for a in base[4].split(",") if a),
                max_legs=int(base[5]),
                expires_at=base[6],
            )
            try:
                direction, before, target = assess_change(base_row, envelope.payload)
                summary = {
                    "direction": direction,
                    "applies": "immediately" if direction == "tighten" else "after_cooling",
                    "current": before.as_dict(),
                    "requested": target.as_dict(),
                }
            except MandateChangeError as exc:
                summary = {"direction": None, "error": exc.code}
        return {
            "authorization_id": authz["authorization_id"],
            "status": authz["status"],
            "operation": authz["operation"],
            "envelope_hash": authz["envelope_hash"],
            "expires_at": authz["expires_at"],
            "reason_codes": authz.get("reason_codes", []),
            "envelope": json.loads(authz["envelope_bytes"]),
            "webauthn_options": drafts.webauthn_request_options(authz),
            "change": {**summary, "cooling_hours": cooling_hours()},
            "scope_note": SCOPE_NOTE,
            "simulated": True,
        }

    # ── Approve (passkey) and commit the change ──────────────────

    def approve_change(
        self,
        *,
        user_id: str,
        authz_id: str,
        expected_envelope_hash: str,
        assertion: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify the passkey assertion over the stored envelope, then apply the change.

        Same checks as a payment approval (owner, operation, expected hash,
        stored-bytes integrity, credential ownership, origin, RP ID, user
        verification, sign count, single use).  Recording the approval evidence,
        the mandate update, cancellations/supersessions, the receipt,
        ``COMMITTED`` and the audit events share one transaction.
        """
        from webauthn import verify_authentication_response

        from gateway.identity import get_identity_store
        from shared.settings import load_gateway_settings

        drafts = self._drafts()
        authz = drafts.get_authorization(authz_id, user_id)
        if authz is None:
            raise MandateError("AUTHORIZATION_NOT_FOUND")
        if authz["operation"] != OPERATION:
            raise MandateError("OPERATION_MISMATCH")
        if not hmac.compare_digest(str(expected_envelope_hash), authz["envelope_hash"]):
            raise MandateError("ENVELOPE_HASH_MISMATCH")
        if authz["status"] == "COMMITTED":
            receipt = drafts.get_receipt(authz_id, user_id)
            if receipt is not None:
                return _receipt_response(authz_id, receipt)
        if authz["status"] != "PENDING_APPROVAL":
            raise MandateError(f"NOT_PENDING: {authz['status']}")
        if from_iso(authz["expires_at"]) <= utcnow():
            self._expire(authz_id, user_id)
            raise MandateError("AUTHORIZATION_EXPIRED")

        envelope = self._stored_envelope(authz)
        cred = get_identity_store().get_credential(authz["credential_id"])
        if (
            cred is None
            or cred["user_id"] != user_id
            or envelope.credential_id != cred["credential_id"]
            or assertion.get("id") != cred["credential_id"]
        ):
            raise MandateError("CREDENTIAL_NOT_OWNED")
        settings = load_gateway_settings()
        try:
            verified = verify_authentication_response(
                credential=assertion,
                expected_challenge=approval_challenge(envelope),
                expected_rp_id=settings.rp_id,
                expected_origin=settings.rp_origin,
                credential_public_key=cred["public_key"],
                credential_current_sign_count=cred["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            self._audit_once(
                user_id,
                "gateway.approval.signature_rejected",
                {"authorization_id": authz_id, "operation": OPERATION, "error": type(exc).__name__},
            )
            raise MandateError("SIGNATURE_INVALID") from None

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = self._commit_locked(conn, user_id, authz_id, envelope, assertion, verified)
            conn.commit()
            return result
        except _Stop as stop:
            if stop.persist:
                conn.commit()
            else:
                conn.rollback()
            if stop.response is not None:
                return stop.response
            raise MandateError(stop.code, stop.reason_codes) from None
        except MandateError:
            if conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise MandateError("COMMIT_FAILED") from None
        finally:
            conn.close()

    def _commit_locked(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        authz_id: str,
        envelope: SigningEnvelope,
        assertion: dict[str, Any],
        verified: Any,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT status, expires_at, envelope_hash FROM authorizations "
            "WHERE authorization_id = ? AND user_id = ?",
            (authz_id, user_id),
        ).fetchone()
        if row is not None and row["status"] == "COMMITTED":
            # A concurrent request committed while this signature was verified.
            from gateway.execution import load_receipt

            receipt = load_receipt(conn, authz_id, user_id)
            if receipt is None:
                raise _Stop("RECEIPT_MISSING")
            raise _Stop("COMMITTED", response=_receipt_response(authz_id, receipt))
        if row is None or row["status"] != "PENDING_APPROVAL":
            raise _Stop(f"NOT_PENDING: {row['status'] if row else None}")
        if conn.execute(
            "SELECT 1 FROM approval_evidence WHERE authorization_id = ?", (authz_id,)
        ).fetchone():
            raise _Stop("ALREADY_APPROVED")

        now = utcnow()
        if min(from_iso(row["expires_at"]), from_iso(envelope.expires_at)) <= now:
            self._set_status(conn, user_id, authz_id, "EXPIRED", ("AUTHORIZATION_EXPIRED",))
            raise _Stop("AUTHORIZATION_EXPIRED", persist=True)

        activate_due_changes(conn, user_id, now, exclude_authorization_id=authz_id)
        current = current_mandate(conn, user_id)
        if current is None or current.version != envelope.active_mandate_version:
            codes = ("MANDATE_VERSION_CHANGED",)
            self._set_status(conn, user_id, authz_id, "SUPERSEDED", codes)
            raise _Stop("AUTHORIZATION_SUPERSEDED", persist=True, reason_codes=codes)
        policy = evaluate_change(conn, envelope, now)
        if policy.decision != "PASS":
            codes = policy.reason_codes or ("UNSUPPORTED_OPERATION",)
            self._set_status(conn, user_id, authz_id, "BLOCKED", codes)
            raise _Stop(
                "POLICY_BLOCK",
                persist=True,
                response=_status_response(authz_id, "BLOCKED", codes),
            )
        assert isinstance(envelope.payload, MandateChange)
        direction, before, target = assess_change(current, envelope.payload)

        # ── Passkey evidence (single use) ──
        conn.execute(
            "INSERT INTO approval_evidence (authorization_id, credential_id, sign_count, "
            "client_data_hash, authenticator_data_hash, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                authz_id,
                envelope.credential_id,
                verified.new_sign_count,
                hashlib.sha256(
                    _b64url(assertion.get("response", {}).get("clientDataJSON", ""))
                ).hexdigest(),
                hashlib.sha256(
                    _b64url(assertion.get("response", {}).get("authenticatorData", ""))
                ).hexdigest(),
                to_iso(now),
            ),
        )
        conn.execute(
            "UPDATE webauthn_credentials SET sign_count = ? WHERE credential_id = ?",
            (verified.new_sign_count, envelope.credential_id),
        )
        _audit(
            conn,
            user_id,
            "gateway.approval.signature_verified",
            {
                "authorization_id": authz_id,
                "operation": OPERATION,
                "credential_id": envelope.credential_id,
                "sign_count": verified.new_sign_count,
                "user_verified": True,
            },
        )

        # ── Apply ──
        change_id = envelope.operation_id
        superseded: list[str] = []
        new_version: int | None = None
        if direction == "tighten":
            cancelled = _cancel_pending(
                conn, user_id, "MANDATE_CHANGE_CANCELLED_BY_TIGHTENING", now
            )
            effective_at = to_iso(now)
            new_version = _insert_version(
                conn, user_id, current, target, effective_at=effective_at, now=now
            )
            change_status = "APPLIED"
            superseded = supersede_pending_authorizations(
                conn, user_id, "MANDATE_TIGHTENED", now, exclude_authorization_id=authz_id
            )
        else:
            cancelled = _cancel_pending(conn, user_id, "MANDATE_CHANGE_REPLACED", now)
            effective_at = to_iso(now + timedelta(hours=cooling_hours()))
            change_status = "PENDING"
        conn.execute(
            "INSERT INTO mandate_changes (change_id, user_id, authorization_id, direction, "
            "base_version, per_leg_cents, daily_cents, allowed_actions, paused, status, "
            "effective_at, resulting_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                change_id,
                user_id,
                authz_id,
                direction,
                current.version,
                target.per_leg_cents,
                target.daily_cents,
                ",".join(target.allowed_actions),
                int(target.paused),
                change_status,
                effective_at,
                new_version,
                to_iso(now),
                to_iso(now),
            ),
        )

        # ── Receipt, state, audit ──
        receipt_id = f"rcpt-{uuid.uuid4().hex[:16]}"
        payload_hash = bundle_hash(envelope.payload)
        document = {
            "receipt_id": receipt_id,
            "authorization_id": authz_id,
            "user_id": user_id,
            "operation": OPERATION,
            "operation_id": change_id,
            "payload_hash": payload_hash,
            "envelope_hash": row["envelope_hash"],
            "committed_at": to_iso(now),
            "direction": direction,
            "outcome": "APPLIED" if direction == "tighten" else "PENDING_COOLING",
            "effective_at": effective_at,
            "base_version": current.version,
            "new_version": new_version,
            "previous": before.as_dict(),
            "settings": target.as_dict(),
            "cancelled_pending_changes": cancelled,
            "superseded_authorizations": superseded,
            "scope_note": SCOPE_NOTE,
            "simulated": True,
        }
        conn.execute(
            "INSERT INTO receipts (receipt_id, authorization_id, user_id, operation, "
            "operation_id, payload_hash, envelope_hash, receipt_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                authz_id,
                user_id,
                OPERATION,
                change_id,
                payload_hash,
                row["envelope_hash"],
                canonical_bytes(document).decode("utf-8"),
                to_iso(now),
            ),
        )
        cur = conn.execute(
            "UPDATE authorizations SET status = 'COMMITTED', status_reason = NULL "
            "WHERE authorization_id = ? AND status = 'PENDING_APPROVAL'",
            (authz_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("authorization status changed under the write lock")
        _audit(
            conn,
            user_id,
            "gateway.mandate.change_committed",
            {
                key: document[key]
                for key in (
                    "authorization_id",
                    "operation_id",
                    "receipt_id",
                    "payload_hash",
                    "envelope_hash",
                    "direction",
                    "outcome",
                    "effective_at",
                    "base_version",
                    "new_version",
                    "settings",
                    "cancelled_pending_changes",
                    "superseded_authorizations",
                )
            },
        )
        return {
            "authorization_id": authz_id,
            "status": "COMMITTED",
            "receipt_id": receipt_id,
            "receipt": document,
            "replayed": False,
        }

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _stored_envelope(authz: dict[str, Any]) -> SigningEnvelope:
        raw = authz["envelope_bytes"]
        try:
            envelope = SigningEnvelope.model_validate(json.loads(raw))
        except Exception:
            raise MandateError("ENVELOPE_INTEGRITY") from None
        if (
            canonical_bytes(envelope) != raw
            or approval_challenge_hex(envelope) != authz["envelope_hash"]
            or envelope.operation != OPERATION
            or not isinstance(envelope.payload, MandateChange)
            or envelope.user_id != authz["user_id"]
            or envelope.operation_id != authz["bundle_id"]
            or envelope.active_mandate_version != authz["active_mandate_version"]
        ):
            raise MandateError("ENVELOPE_INTEGRITY")
        return envelope

    @staticmethod
    def _set_status(
        conn: sqlite3.Connection,
        user_id: str,
        authz_id: str,
        to_status: str,
        codes: tuple[str, ...],
    ) -> None:
        cur = conn.execute(
            "UPDATE authorizations SET status = ?, status_reason = ? "
            "WHERE authorization_id = ? AND user_id = ? AND status = 'PENDING_APPROVAL'",
            (to_status, ",".join(codes), authz_id, user_id),
        )
        if cur.rowcount:
            _audit(
                conn,
                user_id,
                f"gateway.authorization.{to_status.lower()}",
                {"authorization_id": authz_id, "operation": OPERATION, "reason_codes": list(codes)},
            )

    def _expire(self, authz_id: str, user_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._set_status(conn, user_id, authz_id, "EXPIRED", ("AUTHORIZATION_EXPIRED",))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _audit_once(self, user_id: str, step: str, payload: dict[str, Any]) -> None:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _audit(conn, user_id, step, payload)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
