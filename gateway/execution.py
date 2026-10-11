"""Atomic recheck-and-commit of an approved ``execute_bundle`` (T-06).

:func:`commit` is the only code path that moves (simulated) money.  It opens
its own ``BEGIN IMMEDIATE`` write transaction and, under that lock:

1. re-reads the authorization by ID for the authenticated user and rebuilds
   the envelope from the **stored bytes** (never from the request), checking
   the stored hash;
2. applies the idempotency key ``(user_id, operation, operation_id)``: an
   existing receipt with the same payload hash is returned unchanged (even
   after the approval expired); a different payload hash is rejected;
3. requires ``PENDING_APPROVAL`` (or ``AWAITING_STEP_UP`` together with a
   ``step_up_proof``) and recorded passkey approval evidence from the
   credential named in the envelope;
4. expires an approval whose window has passed (``EXPIRED``, no effects);
5. for ``AWAITING_STEP_UP``: runs the step-up proof (T-07,
   ``gateway.stepup.CodeProof``) **under this same lock**.  The proof checks
   the code against the challenge bound to this user, authorization and
   envelope hash and marks it ``CONSUMED`` in this transaction; a wrong code
   counts an attempt (the fifth → ``BLOCKED``) and an expired challenge →
   ``EXPIRED``.  Consumption therefore commits or rolls back with everything
   below;
6. makes a cooled mandate change effective if its time has come (T-07,
   ``gateway.mandates.activate_due_changes``), then supersedes the
   authorization if the conversation revision/hash, the active mandate
   version or the frozen draft no longer match the envelope;
7. re-checks semantic validation (T-10 hook) and re-runs the full
   deterministic policy: ``BLOCK`` → ``BLOCKED`` (even after a valid step-up
   code); ``STEP_UP`` without a verified proof → ``AWAITING_STEP_UP`` plus one
   step-up challenge; neither moves money;
8. applies every leg's effect in order from the stored envelope, writes
   ``transactions`` rows, persists the receipt, marks the authorization
   ``COMMITTED`` and the draft ``EXECUTED``, and appends the audit event.

Any exception rolls back the whole transaction (no partial legs, no
unaudited commit, no consumed step-up proof).  Status changes in steps 4–7
are committed with their own audit event and reported by raising
:class:`ExecutionRefused`; leaving ``AWAITING_STEP_UP`` for any status other
than ``COMMITTED`` also closes its step-up challenge in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway.approval.hooks import Receipt
from gateway.audit import append_audit
from gateway.policy import PASS, STEP_UP, active_mandate_version, evaluate
from ledger.schema import cents_to_money, connect, money_to_cents
from shared.canonical import approval_challenge_hex, bundle_hash, canonical_bytes
from shared.schemas import FrozenBundle, Leg, SigningEnvelope, TargetType
from shared.timeutil import from_iso, to_iso

__all__ = [
    "ExecutionRefused",
    "ExecutionFailed",
    "StepUpProof",
    "StepUpProofResult",
    "commit",
    "load_receipt",
    "PENDING_STATES",
]

OPERATION = "execute_bundle"
PENDING_STATES = ("PENDING_APPROVAL", "AWAITING_STEP_UP")

DbLike = str | Path | sqlite3.Connection | Callable[[], sqlite3.Connection]


@dataclass(frozen=True)
class StepUpProofResult:
    """Outcome of a step-up proof checked under the commit lock.

    ``accepted`` means the proof was valid and has been consumed in the
    current transaction.  Otherwise ``code`` is the stable refusal code and
    ``authorization_status`` the terminal status the authorization must move
    to (``BLOCKED`` / ``EXPIRED``), or ``None`` to stay ``AWAITING_STEP_UP``.
    """

    accepted: bool
    code: str = ""
    authorization_status: str | None = None
    reason_codes: tuple[str, ...] = ()


#: Called with ``(conn, authorization_row, now)`` inside the commit transaction.
StepUpProof = Callable[[sqlite3.Connection, sqlite3.Row, datetime], StepUpProofResult]


class ExecutionRefused(Exception):
    """The recheck refused to execute.  The message is a stable code.

    ``status`` is the authorization's status after the refusal (``None`` if it
    was not read), and ``reason_codes`` carries policy or binding codes.
    """

    def __init__(
        self, code: str, *, status: str | None = None, reason_codes: tuple[str, ...] = ()
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.reason_codes = reason_codes


class ExecutionFailed(Exception):
    """The commit transaction failed and was rolled back; nothing changed."""


class _Refusal(Exception):
    """Internal: stop, then commit (``persist``) or roll back, and refuse."""

    def __init__(
        self,
        code: str,
        *,
        status: str | None = None,
        reason_codes: tuple[str, ...] = (),
        persist: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.reason_codes = reason_codes
        self.persist = persist


# ── Public API ─────────────────────────────────────────────────────


def commit(
    db: DbLike,
    authz: Mapping[str, Any],
    now: datetime,
    *,
    step_up_proof: StepUpProof | None = None,
) -> Receipt:
    """Recheck and atomically commit the authorization identified by *authz*.

    *db* is a database path, a connection factory, or an idle connection.
    Only ``authz["authorization_id"]`` and ``authz["user_id"]`` (the
    authenticated owner) are used; everything else is re-read under the lock.

    ``step_up_proof`` is the T-07 secondary-approval path: it is required for
    an ``AWAITING_STEP_UP`` authorization and refused for any other state.  It
    runs inside this function's write transaction, so proof consumption,
    the policy recheck and the commit are one atomic unit.  There is no
    boolean "already verified" switch.
    """
    from gateway.telemetry import stage

    authz_id, user_id = str(authz["authorization_id"]), str(authz["user_id"])
    with stage(
        "execution",
        attributes={
            "authorization_id": authz_id,
            "operation_type": OPERATION,
            "step_up_required": step_up_proof is not None,
        },
    ) as span:
        conn, owned = _open(db)
        try:
            receipt = _commit(conn, authz_id, user_id, now, step_up_proof)
        except ExecutionRefused as exc:
            refusal_status, reason = _refusal_status(exc)
            span.set_status(refusal_status, reason_code=reason)
            raise
        except ExecutionFailed:
            span.set_status("error", reason_code="COMMIT_FAILED")
            raise
        finally:
            if owned:
                conn.close()
        span.set_attributes({"approval_state": receipt.status})
        _trace_receipt(receipt)
        return receipt


def _refusal_status(exc: ExecutionRefused) -> tuple[str, str | None]:
    """Telemetry status of a refused commit: expected outcomes, not crashes."""
    status = {
        "BLOCKED": "blocked",
        "EXPIRED": "expired",
        "SUPERSEDED": "superseded",
        "CANCELLED": "cancelled",
        "AWAITING_STEP_UP": "pending",
    }.get(exc.status or "", "rejected")
    if exc.code == "STEP_UP_CODE_INVALID":
        status = "rejected"
    reason = exc.reason_codes[0] if status == "blocked" and exc.reason_codes else exc.code
    return status, reason


def _trace_receipt(receipt: Receipt) -> None:
    """A short ``receipt`` span: what was committed (or replayed), no account numbers."""
    from gateway.telemetry import stage

    details = receipt.details or {}
    total = details.get("total_debit")
    with stage(
        "receipt",
        attributes={
            "authorization_id": receipt.authorization_id,
            "bundle_id": details.get("operation_id"),
            "envelope_hash": details.get("envelope_hash"),
            "leg_count": len(details.get("legs") or []),
            "amount": total if isinstance(total, str) else None,
            "currency": "SGD",
            "step_up_required": bool(details.get("step_up_verified")),
        },
    ) as span:
        if receipt.replayed:
            span.set_status("ok", reason_code="RECEIPT_REPLAYED")


def load_receipt(conn: sqlite3.Connection, authorization_id: str, user_id: str) -> Receipt | None:
    """The stored receipt for an authorization owned by *user_id*, if committed."""
    row = conn.execute(
        "SELECT authorization_id, receipt_id, receipt_json FROM receipts "
        "WHERE authorization_id = ? AND user_id = ?",
        (authorization_id, user_id),
    ).fetchone()
    return _receipt_from_row(row, replayed=True) if row is not None else None


# ── Transaction driver ─────────────────────────────────────────────


def _open(db: DbLike) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        return db, False
    if isinstance(db, (str, Path)):
        return connect(db), True
    return db(), True


def _commit(
    conn: sqlite3.Connection,
    authz_id: str,
    user_id: str,
    now: datetime,
    step_up_proof: StepUpProof | None,
) -> Receipt:
    if conn.in_transaction:
        raise RuntimeError("execution.commit needs a connection with no open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt = _commit_locked(conn, authz_id, user_id, now, step_up_proof)
        conn.commit()
        return receipt
    except _Refusal as refusal:
        try:
            if refusal.persist:
                conn.commit()
            else:
                conn.rollback()
        except Exception as exc:
            if conn.in_transaction:
                conn.rollback()
            raise ExecutionFailed("COMMIT_FAILED") from exc
        raise ExecutionRefused(
            refusal.code, status=refusal.status, reason_codes=refusal.reason_codes
        ) from None
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        raise ExecutionFailed("COMMIT_FAILED") from exc


def _commit_locked(
    conn: sqlite3.Connection,
    authz_id: str,
    user_id: str,
    now: datetime,
    step_up_proof: StepUpProof | None,
) -> Receipt:
    from gateway.approval.hooks import _call_semantic_validation_passed
    from gateway.mandates import activate_due_changes
    from gateway.stepup import issue_challenge_locked

    row = conn.execute(
        "SELECT authorization_id, bundle_id, user_id, credential_id, operation, "
        "envelope_bytes, envelope_hash, status, expires_at "
        "FROM authorizations WHERE authorization_id = ?",
        (authz_id,),
    ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise _Refusal("AUTHORIZATION_NOT_FOUND")
    if row["operation"] != OPERATION:
        raise _Refusal("OPERATION_MISMATCH", status=row["status"])

    envelope = _stored_envelope(row)
    bundle = envelope.payload
    if not isinstance(bundle, FrozenBundle):  # _stored_envelope guarantees this
        raise _Refusal("ENVELOPE_INTEGRITY", status=row["status"])
    payload_hash = bundle_hash(bundle)
    status = row["status"]

    # ── Idempotency: (user_id, operation, operation_id) + payload hash ──
    existing = conn.execute(
        "SELECT authorization_id, receipt_id, payload_hash, receipt_json FROM receipts "
        "WHERE user_id = ? AND operation = ? AND operation_id = ?",
        (user_id, OPERATION, envelope.operation_id),
    ).fetchone()
    if existing is not None:
        if existing["payload_hash"] != payload_hash:
            raise _Refusal("IDEMPOTENCY_CONFLICT", status=status)
        if existing["authorization_id"] != authz_id:
            if status not in PENDING_STATES:
                raise _Refusal("NOT_PENDING", status=status)
            # A second live authorization for an operation that already committed:
            # retire it and answer with the original receipt.
            _transition(conn, row, envelope, status, "SUPERSEDED", ("OPERATION_ALREADY_COMMITTED",))
        return _receipt_from_row(existing, replayed=True)
    if status == "COMMITTED":
        raise _Refusal("RECEIPT_MISSING", status=status)  # inconsistent; fail closed

    # ── State ──
    if status == "AWAITING_STEP_UP":
        if step_up_proof is None:
            raise _Refusal("STEP_UP_REQUIRED", status=status)
    elif status != "PENDING_APPROVAL":
        raise _Refusal("NOT_PENDING", status=status)
    elif step_up_proof is not None:
        raise _Refusal("STEP_UP_NOT_REQUIRED", status=status)
    step_up_done = False

    # ── Recorded passkey approval from the envelope's credential, still the user's ──
    evidence = conn.execute(
        "SELECT e.credential_id, c.user_id FROM approval_evidence e "
        "LEFT JOIN webauthn_credentials c ON c.credential_id = e.credential_id "
        "WHERE e.authorization_id = ?",
        (authz_id,),
    ).fetchone()
    if evidence is None:
        raise _Refusal("APPROVAL_REQUIRED", status=status)
    if (
        evidence["credential_id"] != row["credential_id"]
        or envelope.credential_id != row["credential_id"]
        or evidence["user_id"] != user_id
    ):
        raise _Refusal("CREDENTIAL_NOT_OWNED", status=status)

    # ── Expiry: an uncommitted expired approval never executes ──
    if min(from_iso(row["expires_at"]), from_iso(envelope.expires_at)) <= now:
        _transition(conn, row, envelope, status, "EXPIRED", ("AUTHORIZATION_EXPIRED",))
        raise _Refusal("AUTHORIZATION_EXPIRED", status="EXPIRED", persist=True)

    # ── Step-up proof (T-07): verified and consumed under this lock ──
    if step_up_proof is not None:
        proof = step_up_proof(conn, row, now)
        if not proof.accepted:
            # A counted attempt or a closed challenge is persisted with the refusal.
            if proof.authorization_status is not None:
                _transition(
                    conn, row, envelope, status, proof.authorization_status, proof.reason_codes
                )
            raise _Refusal(
                proof.code or "STEP_UP_REJECTED",
                status=proof.authorization_status or status,
                reason_codes=proof.reason_codes,
                persist=True,
            )
        step_up_done = True

    # ── A cooled mandate change whose time has come takes effect first ──
    activate_due_changes(conn, user_id, now, exclude_authorization_id=authz_id)

    # ── Binding: conversation revision, active mandate version, frozen draft ──
    stale = _stale_bindings(conn, envelope, payload_hash)
    if stale:
        _transition(conn, row, envelope, status, "SUPERSEDED", stale)
        raise _Refusal(
            "AUTHORIZATION_SUPERSEDED", status="SUPERSEDED", reason_codes=stale, persist=True
        )

    # ── Semantic validation (T-10) and the full deterministic policy ──
    if not _call_semantic_validation_passed(conn, bundle):
        codes: tuple[str, ...] = ("SEMANTIC_VALIDATION_REQUIRED",)
        _transition(conn, row, envelope, status, "BLOCKED", codes)
        raise _Refusal("POLICY_BLOCK", status="BLOCKED", reason_codes=codes, persist=True)

    from gateway.telemetry import record_policy, stage

    with stage(
        "policy",
        attributes={"bundle_id": envelope.operation_id, "operation_type": OPERATION},
    ) as policy_span:
        outcome = evaluate(conn, envelope, now)
        record_policy(policy_span, outcome.decision, outcome.reason_codes)
    if outcome.decision not in (PASS, STEP_UP):
        codes = outcome.reason_codes or ("POLICY_UNKNOWN_DECISION",)
        _transition(conn, row, envelope, status, "BLOCKED", codes)
        raise _Refusal("POLICY_BLOCK", status="BLOCKED", reason_codes=codes, persist=True)
    if outcome.decision == STEP_UP and not step_up_done:
        if status == "PENDING_APPROVAL":
            _transition(conn, row, envelope, status, "AWAITING_STEP_UP", outcome.reason_codes)
            # Exactly one simulated out-of-band challenge, in this transaction.
            issue_challenge_locked(
                conn,
                authorization_id=authz_id,
                user_id=user_id,
                envelope_hash=row["envelope_hash"],
                expires_at=min(row["expires_at"], envelope.expires_at, key=from_iso),
                conversation_id=envelope.conversation_id,
                conversation_revision=envelope.conversation_revision,
                now=now,
            )
        raise _Refusal(
            "STEP_UP_REQUIRED",
            status="AWAITING_STEP_UP",
            reason_codes=outcome.reason_codes,
            persist=True,
        )

    # ── Effects, receipt, state, audit: all in this transaction ──
    legs = _apply_effects(conn, authz_id, user_id, bundle, now)
    receipt_id = f"rcpt-{uuid.uuid4().hex[:16]}"
    total_cents = sum(money_to_cents(leg.amount) for leg in bundle.legs)
    document = {
        "receipt_id": receipt_id,
        "authorization_id": authz_id,
        "user_id": user_id,
        "operation": OPERATION,
        "operation_id": envelope.operation_id,
        "payload_hash": payload_hash,
        "envelope_hash": row["envelope_hash"],
        "committed_at": to_iso(now),
        "step_up_verified": step_up_done,
        "total_debit": cents_to_money(total_cents).to_str(),
        "legs": legs,
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
            envelope.operation_id,
            payload_hash,
            row["envelope_hash"],
            canonical_bytes(document).decode("utf-8"),
            to_iso(now),
        ),
    )
    _set_status(conn, authz_id, status, "COMMITTED", None)
    executed = conn.execute(
        "UPDATE frozen_drafts SET status = 'EXECUTED' "
        "WHERE bundle_id = ? AND owner_user_id = ? AND status = 'DRAFT_FROZEN'",
        (envelope.operation_id, user_id),
    )
    if executed.rowcount != 1:
        raise RuntimeError("frozen draft changed under the write lock")
    append_audit(
        conn,
        source="gateway",
        conversation_id=envelope.conversation_id,
        user_id=user_id,
        step="gateway.execution.commit",
        payload={
            "authorization_id": authz_id,
            "operation": OPERATION,
            "operation_id": envelope.operation_id,
            "receipt_id": receipt_id,
            "payload_hash": payload_hash,
            "envelope_hash": row["envelope_hash"],
            "step_up_verified": step_up_done,
            "total_debit": document["total_debit"],
            "legs": [
                {
                    "leg_id": leg["leg_id"],
                    "action": leg["action"],
                    "amount": leg["amount"],
                    "transaction_id": leg["transaction_id"],
                }
                for leg in legs
            ],
        },
    )
    return Receipt(
        authorization_id=authz_id,
        receipt_id=receipt_id,
        status="COMMITTED",
        details=document,
        replayed=False,
    )


# ── Checks ─────────────────────────────────────────────────────────


def _stored_envelope(row: sqlite3.Row) -> SigningEnvelope:
    """Rebuild the envelope from the stored bytes and prove they are intact."""
    raw = bytes(row["envelope_bytes"])
    try:
        envelope = SigningEnvelope.model_validate(json.loads(raw))
    except Exception:
        raise _Refusal("ENVELOPE_INTEGRITY", status=row["status"]) from None
    if (
        canonical_bytes(envelope) != raw
        or approval_challenge_hex(envelope) != row["envelope_hash"]
        or envelope.operation != OPERATION
        or not isinstance(envelope.payload, FrozenBundle)
        or envelope.user_id != row["user_id"]
        or envelope.operation_id != str(envelope.payload.bundle_id)
        or envelope.operation_id != row["bundle_id"]
    ):
        raise _Refusal("ENVELOPE_INTEGRITY", status=row["status"])
    return envelope


def _stale_bindings(
    conn: sqlite3.Connection, envelope: SigningEnvelope, payload_hash: str
) -> tuple[str, ...]:
    stale: list[str] = []
    conv = conn.execute(
        "SELECT owner_user_id, revision, conversation_hash FROM conversations "
        "WHERE conversation_id = ?",
        (envelope.conversation_id,),
    ).fetchone()
    if (
        conv is None
        or conv["owner_user_id"] != envelope.user_id
        or int(conv["revision"]) != envelope.conversation_revision
        or conv["conversation_hash"] != envelope.conversation_hash
    ):
        stale.append("CONVERSATION_CHANGED")
    if active_mandate_version(conn, envelope.user_id) != envelope.active_mandate_version:
        stale.append("MANDATE_VERSION_CHANGED")
    draft = conn.execute(
        "SELECT owner_user_id, status, bundle_hash FROM frozen_drafts WHERE bundle_id = ?",
        (envelope.operation_id,),
    ).fetchone()
    if (
        draft is None
        or draft["owner_user_id"] != envelope.user_id
        or draft["status"] != "DRAFT_FROZEN"
        or draft["bundle_hash"] != payload_hash
    ):
        stale.append("DRAFT_NOT_ELIGIBLE")
    return tuple(stale)


# ── State changes ──────────────────────────────────────────────────


def _set_status(
    conn: sqlite3.Connection,
    authz_id: str,
    from_status: str,
    to_status: str,
    reason_codes: tuple[str, ...] | None,
) -> None:
    cur = conn.execute(
        "UPDATE authorizations SET status = ?, status_reason = ? "
        "WHERE authorization_id = ? AND status = ?",
        (to_status, ",".join(reason_codes) if reason_codes else None, authz_id, from_status),
    )
    if cur.rowcount != 1:
        raise RuntimeError("authorization status changed under the write lock")


def _transition(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    envelope: SigningEnvelope,
    from_status: str,
    to_status: str,
    reason_codes: tuple[str, ...],
) -> None:
    _set_status(conn, row["authorization_id"], from_status, to_status, reason_codes)
    if from_status == "AWAITING_STEP_UP" and to_status != "COMMITTED":
        from gateway.clock import utcnow
        from gateway.stepup import close_for_authorization_locked

        close_for_authorization_locked(
            conn, row["authorization_id"], to_status, reason_codes, utcnow()
        )
    append_audit(
        conn,
        source="gateway",
        conversation_id=envelope.conversation_id,
        user_id=row["user_id"],
        step=f"gateway.execution.{to_status.lower()}",
        payload={
            "authorization_id": row["authorization_id"],
            "operation_id": envelope.operation_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason_codes": list(reason_codes),
        },
    )


# ── Effects ────────────────────────────────────────────────────────


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _move_balance(
    conn: sqlite3.Connection, user_id: str, account_id: str, delta_cents: int
) -> None:
    """Add *delta_cents* to an account the user holds; never below zero."""
    cur = conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ? "
        "WHERE account_id = ? AND balance_cents + ? >= 0 AND EXISTS ("
        "SELECT 1 FROM account_holders WHERE account_id = ? AND user_id = ?)",
        (delta_cents, account_id, delta_cents, account_id, user_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"balance guard failed for {account_id}")


def _insert_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    account_id: str,
    leg: Leg,
    leg_index: int,
    authz_id: str,
    bundle_id: str,
    direction: str,
    created_at: str,
    settlement_ref: str | None = None,
) -> str:
    txn_id = _new_id("txn")
    terms = leg.equity_terms if direction == "debit" else None
    conn.execute(
        "INSERT INTO transactions (transaction_id, user_id, account_id, action, target_type, "
        "target_id, amount_cents, status, created_at, authorization_id, bundle_id, leg_id, "
        "leg_index, direction, quantity, quote_id, settlement_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'COMMITTED', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            txn_id,
            user_id,
            account_id,
            leg.action.value,
            leg.target_type.value,
            leg.target_id,
            money_to_cents(leg.amount),
            created_at,
            authz_id,
            bundle_id,
            leg.leg_id,
            leg_index,
            direction,
            terms.quantity if terms else None,
            terms.quote_id if terms else None,
            settlement_ref,
        ),
    )
    return txn_id


def _apply_effects(
    conn: sqlite3.Connection,
    authz_id: str,
    user_id: str,
    bundle: FrozenBundle,
    now: datetime,
) -> list[dict[str, Any]]:
    """Apply every leg in order from the frozen bundle; return receipt leg entries.

    Guards repeat the policy's checks at the SQL level (holder, balance,
    outstanding), so a mismatch raises and rolls the whole bundle back.
    """
    ts = to_iso(now)
    bundle_id = str(bundle.bundle_id)
    out: list[dict[str, Any]] = []
    for index, leg in enumerate(bundle.legs):
        amount = money_to_cents(leg.amount)
        credit_txn: str | None = None
        external = leg.target_type in (
            TargetType.saved_payee,
            TargetType.saved_paynow,
            TargetType.biller,
        )
        # Payees, PayNow recipients and billers get a simulated settlement reference.
        settlement_ref = f"SIM-{uuid.uuid4().hex[:12].upper()}" if external else None

        _move_balance(conn, user_id, leg.source_account_id, -amount)
        debit_txn = _insert_transaction(
            conn,
            user_id=user_id,
            account_id=leg.source_account_id,
            leg=leg,
            leg_index=index,
            authz_id=authz_id,
            bundle_id=bundle_id,
            direction="debit",
            created_at=ts,
            settlement_ref=settlement_ref,
        )
        if leg.target_type == TargetType.own_account:
            _move_balance(conn, user_id, leg.target_id, amount)
            credit_txn = _insert_transaction(
                conn,
                user_id=user_id,
                account_id=leg.target_id,
                leg=leg,
                leg_index=index,
                authz_id=authz_id,
                bundle_id=bundle_id,
                direction="credit",
                created_at=ts,
            )
        elif leg.target_type == TargetType.owned_card:
            cur = conn.execute(
                "UPDATE cards SET outstanding_cents = outstanding_cents - ? "
                "WHERE card_id = ? AND owner_user_id = ? AND outstanding_cents >= ?",
                (amount, leg.target_id, user_id, amount),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"card guard failed for {leg.target_id}")
        elif leg.target_type == TargetType.sgx_symbol:
            # Frozen terms only: quantity and actual debit are never recomputed.
            terms = leg.equity_terms
            if terms is None or money_to_cents(terms.actual_debit) != amount:
                raise RuntimeError("equity terms do not match the leg debit")
            conn.execute(
                "INSERT INTO holdings (holding_id, owner_user_id, symbol, quantity, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (owner_user_id, symbol) DO UPDATE SET "
                "quantity = quantity + excluded.quantity, updated_at = excluded.updated_at",
                (_new_id("hold"), user_id, leg.target_id.upper(), terms.quantity, ts),
            )

        entry: dict[str, Any] = {
            "leg_id": leg.leg_id,
            "leg_index": index,
            "action": leg.action.value,
            "source_account_id": leg.source_account_id,
            "target_type": leg.target_type.value,
            "target_id": leg.target_id,
            "display_name": leg.display_metadata.display_name,
            "amount": leg.amount.to_str(),
            "transaction_id": debit_txn,
            "credit_transaction_id": credit_txn,
            "settlement_ref": settlement_ref,
        }
        if leg.equity_terms is not None:
            entry["quantity"] = leg.equity_terms.quantity
            entry["unit_price"] = leg.equity_terms.unit_price.to_str()
            entry["quote_id"] = leg.equity_terms.quote_id
        out.append(entry)
    return out


def _receipt_from_row(row: sqlite3.Row, *, replayed: bool) -> Receipt:
    return Receipt(
        authorization_id=row["authorization_id"],
        receipt_id=row["receipt_id"],
        status="COMMITTED",
        details=json.loads(row["receipt_json"]),
        replayed=replayed,
    )
