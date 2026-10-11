"""Simulated out-of-band step-up (secondary approval) for payments (T-07).

**This is a simulation.**  The "out-of-band channel" is a demo operator
console (``gateway/operator_routes.py``) protected by its own credential; it
stands in for an SMS/app push to the user's phone.  It is not production MFA,
and nothing here claims otherwise.

Lifecycle
---------
- **Issue.**  When a payment approval with a verified passkey assertion ends
  in ``AWAITING_STEP_UP`` (elevated risk inside every hard limit), exactly one
  challenge is created for that authorization, in the same transaction as the
  status change (:func:`enter_step_up` for the approval pre-check,
  :func:`issue_challenge_locked` from ``gateway.execution``).  It is bound to
  the user, the authorization and its envelope hash.  A repeated approval
  returns the pending status and never issues a second code.
- **Code.**  Six digits derived as ``HMAC-SHA256(secret, challenge_id, salt)``
  from a random per-challenge salt and a gateway-held secret
  (``STEP_UP_SECRET``, else a random per-process secret).  The code itself is
  never stored, logged, audited or returned to the user's session: the table
  keeps only the salt and a keyed verifier ``HMAC-SHA256(secret, user,
  authorization, envelope hash, challenge, salt, code)``.  Without the secret
  the 10^6 code space cannot be searched offline from the database.  The
  operator route recomputes the code to "deliver" it.
- **Verify.**  ``POST /api/authorizations/{id}/step-up`` passes a
  :class:`CodeProof` to :func:`gateway.execution.commit`, which runs it under
  its ``BEGIN IMMEDIATE`` lock: a valid code marks the challenge ``CONSUMED``
  in the same transaction as the full policy recheck and the commit.  If the
  recheck blocks, expires or supersedes the authorization, that terminal
  status commits together with the consumption, so the code can never be used
  again, for this or any other outcome.  If the commit transaction rolls back
  (``ExecutionFailed``), the consumption rolls back with it and
  :meth:`StepUpService.submit` then invalidates the challenge in a separate
  transaction (fail closed: the user requests a fresh approval).
- **Limits.**  At most five failed attempts (the fifth blocks the challenge
  and the authorization); single use; re-delivery ("resend") never extends
  the expiry.  Concurrent submissions serialize on the write lock: the first
  valid one commits, the rest see ``COMMITTED`` and get the stored receipt.
- **Invalidation.**  Leaving ``AWAITING_STEP_UP`` for anything but
  ``COMMITTED`` closes the challenge: policy block, expiry, a conversation
  correction (registered in ``gateway.audit.REVISION_BOUND_TABLES``), a
  mandate decrease/pause or activation (``gateway.mandates``), or a cancel.

Expiry choice
-------------
The challenge expires with the authorization: ``min(authorization.expires_at,
envelope.expires_at)``, at most 120 s after the envelope was issued.  The
user signed that expiry, and ``gateway.execution`` refuses to commit after it,
so a longer step-up window could never complete.  For a demo this leaves
roughly 1–2 minutes to read the code from the operator console; if it lapses
the authorization becomes ``EXPIRED`` and the user requests a fresh envelope
(new passkey approval, new challenge).  Nothing is extended on resend.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.audit import REVISION_BOUND_TABLES, append_audit
from gateway.clock import utcnow
from gateway.execution import ExecutionFailed, ExecutionRefused, StepUpProofResult, commit
from ledger.schema import connect
from shared.timeutil import from_iso, to_iso

__all__ = [
    "SIMULATION_LABEL",
    "MAX_ATTEMPTS",
    "CODE_DIGITS",
    "REASON_CODES",
    "StepUpError",
    "StepUpService",
    "CodeProof",
    "enter_step_up",
    "issue_challenge_locked",
    "close_for_authorization_locked",
    "operator_token_status",
    "load_step_up_settings",
]

logger = logging.getLogger("dcta.gateway.stepup")

SIMULATION_LABEL = (
    "SIMULATED secondary channel (demo operator console). Not production MFA; "
    "no SMS, push or bank channel is involved."
)
MAX_ATTEMPTS = 5
CODE_DIGITS = 6
MIN_SECRET_LEN = 32
_CODE_RE = re.compile(r"\d{6}")
_CODE_DOMAIN = b"DCTA:stepup-code:v1\x00"
_VERIFIER_DOMAIN = b"DCTA:stepup-verifier:v1\x00"

#: Stable step-up reason codes.  Clients key on these, never on prose.
REASON_CODES: dict[str, str] = {
    "STEP_UP_CODE_INVALID": "The code is wrong.",
    "STEP_UP_ATTEMPTS_EXHAUSTED": "Too many wrong codes; the payment is blocked.",
    "STEP_UP_EXPIRED": "The step-up code expired with the approval window.",
    "STEP_UP_BINDING_MISMATCH": "The challenge is not bound to this authorization.",
    "STEP_UP_NOT_ISSUED": "No step-up challenge exists for this authorization.",
    "STEP_UP_NOT_ACTIVE": "The step-up challenge is no longer usable.",
    "STEP_UP_NOT_REQUIRED": "This authorization is not awaiting step-up.",
    "STEP_UP_CODE_UNAVAILABLE": "The simulated channel cannot reproduce this code.",
    "USER_CANCELLED": "The user cancelled the authorization.",
    "COMMIT_FAILED": "The commit rolled back; the step-up code was invalidated.",
}

#: Refusal codes meaning "this submission moved the authorization to BLOCKED".
_BLOCKED_NOW = frozenset({"POLICY_BLOCK", "STEP_UP_ATTEMPTS_EXHAUSTED", "STEP_UP_BINDING_MISMATCH"})

#: Challenge status when its authorization leaves AWAITING_STEP_UP (else INVALIDATED).
_CHALLENGE_STATUS_FOR = {
    "EXPIRED": "EXPIRED",
    "SUPERSEDED": "SUPERSEDED",
    "CANCELLED": "CANCELLED",
}

# A conversation correction supersedes the challenge with its authorization.
REVISION_BOUND_TABLES["step_up_challenges"] = {
    "status_column": "status",
    "revision_column": "conversation_revision",
    "conversation_id_column": "conversation_id",
    "superseded_value": "SUPERSEDED",
}


class StepUpError(Exception):
    """A step-up request was refused.  The message is a stable code."""

    def __init__(
        self,
        code: str,
        reason_codes: tuple[str, ...] = (),
        *,
        status: str | None = None,
        attempts_remaining: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = tuple(reason_codes)
        self.status = status
        self.attempts_remaining = attempts_remaining


# ── Settings and secret ────────────────────────────────────────────


class StepUpSettings(BaseSettings):
    """Gateway-only secrets of the simulated channel.

    ``OPERATOR_TOKEN`` protects the operator console; unset or shorter than 32
    characters disables it.  ``STEP_UP_SECRET`` keys code derivation and the
    verifier; unset means a random per-process secret (codes then do not
    survive a restart).  Neither is given to the agent or validator.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    operator_token: SecretStr | None = Field(default=None, alias="OPERATOR_TOKEN")
    step_up_secret: SecretStr | None = Field(default=None, alias="STEP_UP_SECRET")


def load_step_up_settings() -> StepUpSettings:
    return StepUpSettings()


def _usable(secret: SecretStr | None) -> str | None:
    value = secret.get_secret_value() if secret is not None else ""
    if len(value) < MIN_SECRET_LEN or value.startswith("replace-with"):
        return None
    return value


def operator_token_status() -> str | None:
    """The configured operator token, or ``None`` when the console is disabled."""
    return _usable(load_step_up_settings().operator_token)


_secret: bytes | None = None
_secret_lock = threading.Lock()


def _code_secret() -> bytes:
    global _secret
    with _secret_lock:
        if _secret is None:
            configured = _usable(load_step_up_settings().step_up_secret)
            if configured is not None:
                _secret = configured.encode("utf-8")
            else:
                logger.warning(
                    "STEP_UP_SECRET is not set (or shorter than %d chars); using a random "
                    "per-process secret. Pending step-up codes will not survive a restart.",
                    MIN_SECRET_LEN,
                )
                _secret = secrets.token_bytes(32)
        return _secret


def _set_secret(secret: bytes | None) -> None:
    """Test hook: replace the code secret (``None`` reloads it lazily)."""
    global _secret
    with _secret_lock:
        _secret = secret


def _join(*parts: str) -> bytes:
    return b"\x00".join(part.encode("utf-8") for part in parts)


def _derive_code(challenge_id: str, salt: str) -> str:
    mac = hmac.new(_code_secret(), _CODE_DOMAIN + _join(challenge_id, salt), hashlib.sha256)
    value = int.from_bytes(mac.digest()[:8], "big") % (10**CODE_DIGITS)
    return f"{value:0{CODE_DIGITS}d}"


def _verifier(
    *,
    user_id: str,
    authorization_id: str,
    envelope_hash: str,
    challenge_id: str,
    salt: str,
    code: str,
) -> str:
    body = _join(user_id, authorization_id, envelope_hash, challenge_id, salt, code)
    return hmac.new(_code_secret(), _VERIFIER_DOMAIN + body, hashlib.sha256).hexdigest()


# ── Locked primitives (caller holds BEGIN IMMEDIATE) ───────────────


def issue_challenge_locked(
    conn: sqlite3.Connection,
    *,
    authorization_id: str,
    user_id: str,
    envelope_hash: str,
    expires_at: str,
    conversation_id: str | None,
    conversation_revision: int | None,
    now: datetime,
) -> str:
    """Issue the authorization's one challenge; return its ID (existing one if any)."""
    existing = conn.execute(
        "SELECT challenge_id FROM step_up_challenges WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    challenge_id = f"stepup-{uuid.uuid4().hex[:16]}"
    salt = secrets.token_hex(16)
    verifier = _verifier(
        user_id=user_id,
        authorization_id=authorization_id,
        envelope_hash=envelope_hash,
        challenge_id=challenge_id,
        salt=salt,
        code=_derive_code(challenge_id, salt),
    )
    conn.execute(
        "INSERT INTO step_up_challenges (challenge_id, authorization_id, user_id, envelope_hash, "
        "conversation_id, conversation_revision, salt, verifier, status, max_attempts, "
        "issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
        (
            challenge_id,
            authorization_id,
            user_id,
            envelope_hash,
            conversation_id,
            conversation_revision,
            salt,
            verifier,
            MAX_ATTEMPTS,
            to_iso(now),
            expires_at,
        ),
    )
    append_audit(
        conn,
        source="gateway",
        conversation_id=conversation_id,
        user_id=user_id,
        step="gateway.stepup.issued",
        payload={
            "authorization_id": authorization_id,
            "challenge_id": challenge_id,
            "envelope_hash": envelope_hash,
            "expires_at": expires_at,
            "max_attempts": MAX_ATTEMPTS,
            "channel": "simulated-operator-console",
        },
    )
    return challenge_id


def _close(
    conn: sqlite3.Connection, challenge: sqlite3.Row, status: str, reason: str, now: datetime
) -> bool:
    cur = conn.execute(
        "UPDATE step_up_challenges SET status = ?, status_reason = ?, closed_at = ? "
        "WHERE challenge_id = ? AND status = 'PENDING'",
        (status, reason, to_iso(now), challenge["challenge_id"]),
    )
    if cur.rowcount:
        append_audit(
            conn,
            source="gateway",
            conversation_id=challenge["conversation_id"],
            user_id=challenge["user_id"],
            step=f"gateway.stepup.{status.lower()}",
            payload={
                "authorization_id": challenge["authorization_id"],
                "challenge_id": challenge["challenge_id"],
                "failed_attempts": int(challenge["failed_attempts"]),
                "reason_codes": [code for code in reason.split(",") if code],
            },
        )
    return bool(cur.rowcount)


def close_for_authorization_locked(
    conn: sqlite3.Connection,
    authorization_id: str,
    authorization_status: str,
    reason_codes: tuple[str, ...],
    now: datetime,
) -> bool:
    """Close a still-pending challenge because its authorization left AWAITING_STEP_UP."""
    challenge = conn.execute(
        "SELECT * FROM step_up_challenges WHERE authorization_id = ? AND status = 'PENDING'",
        (authorization_id,),
    ).fetchone()
    if challenge is None:
        return False
    status = _CHALLENGE_STATUS_FOR.get(authorization_status, "INVALIDATED")
    return _close(conn, challenge, status, ",".join(reason_codes) or authorization_status, now)


def _verify_locked(
    conn: sqlite3.Connection, authz: sqlite3.Row, code: str, now: datetime
) -> StepUpProofResult:
    """Check *code* for the authorization row *authz*; consume it when valid."""
    challenge = conn.execute(
        "SELECT * FROM step_up_challenges WHERE authorization_id = ?",
        (authz["authorization_id"],),
    ).fetchone()
    if challenge is None:
        return StepUpProofResult(False, "STEP_UP_NOT_ISSUED", reason_codes=("STEP_UP_NOT_ISSUED",))
    if challenge["status"] != "PENDING":
        return StepUpProofResult(False, "STEP_UP_NOT_ACTIVE", reason_codes=("STEP_UP_NOT_ACTIVE",))
    if challenge["user_id"] != authz["user_id"] or not hmac.compare_digest(
        challenge["envelope_hash"], authz["envelope_hash"]
    ):
        _close(conn, challenge, "BLOCKED", "STEP_UP_BINDING_MISMATCH", now)
        codes = ("STEP_UP_BINDING_MISMATCH",)
        return StepUpProofResult(False, codes[0], "BLOCKED", codes)
    if from_iso(challenge["expires_at"]) <= now:
        _close(conn, challenge, "EXPIRED", "STEP_UP_EXPIRED", now)
        return StepUpProofResult(False, "STEP_UP_EXPIRED", "EXPIRED", ("STEP_UP_EXPIRED",))

    candidate = code if _CODE_RE.fullmatch(code) else "invalid"
    expected = _verifier(
        user_id=authz["user_id"],
        authorization_id=authz["authorization_id"],
        envelope_hash=authz["envelope_hash"],
        challenge_id=challenge["challenge_id"],
        salt=challenge["salt"],
        code=candidate,
    )
    if not hmac.compare_digest(expected, challenge["verifier"]):
        failed = int(challenge["failed_attempts"]) + 1
        exhausted = failed >= int(challenge["max_attempts"])
        conn.execute(
            "UPDATE step_up_challenges SET failed_attempts = ? WHERE challenge_id = ?",
            (failed, challenge["challenge_id"]),
        )
        challenge = conn.execute(
            "SELECT * FROM step_up_challenges WHERE challenge_id = ?", (challenge["challenge_id"],)
        ).fetchone()
        if exhausted:
            _close(conn, challenge, "BLOCKED", "STEP_UP_ATTEMPTS_EXHAUSTED", now)
            codes = ("STEP_UP_ATTEMPTS_EXHAUSTED",)
            return StepUpProofResult(False, codes[0], "BLOCKED", codes)
        append_audit(
            conn,
            source="gateway",
            conversation_id=challenge["conversation_id"],
            user_id=challenge["user_id"],
            step="gateway.stepup.failed_attempt",
            payload={
                "authorization_id": challenge["authorization_id"],
                "challenge_id": challenge["challenge_id"],
                "failed_attempts": failed,
                "attempts_remaining": int(challenge["max_attempts"]) - failed,
            },
        )
        return StepUpProofResult(
            False, "STEP_UP_CODE_INVALID", reason_codes=("STEP_UP_CODE_INVALID",)
        )

    cur = conn.execute(
        "UPDATE step_up_challenges SET status = 'CONSUMED', status_reason = 'VERIFIED', "
        "closed_at = ? WHERE challenge_id = ? AND status = 'PENDING'",
        (to_iso(now), challenge["challenge_id"]),
    )
    if cur.rowcount != 1:
        raise RuntimeError("step-up challenge changed under the write lock")
    append_audit(
        conn,
        source="gateway",
        conversation_id=challenge["conversation_id"],
        user_id=challenge["user_id"],
        step="gateway.stepup.verified",
        payload={
            "authorization_id": challenge["authorization_id"],
            "challenge_id": challenge["challenge_id"],
            "failed_attempts": int(challenge["failed_attempts"]),
        },
    )
    return StepUpProofResult(True)


class CodeProof:
    """A submitted step-up code, checked by ``execution.commit`` under its lock.

    ``accepted`` records whether the code was valid (and consumed in the
    commit transaction).  The code is kept only in memory for this request and
    never appears in ``repr``.
    """

    def __init__(self, code: str) -> None:
        self._code = code
        self.accepted = False

    def __call__(
        self, conn: sqlite3.Connection, authz: sqlite3.Row, now: datetime
    ) -> StepUpProofResult:
        result = _verify_locked(conn, authz, self._code, now)
        self.accepted = result.accepted
        return result

    def __repr__(self) -> str:
        return "CodeProof(<redacted>)"


def enter_step_up(
    db_path: str, authorization_id: str, user_id: str, reason_codes: tuple[str, ...]
) -> bool:
    """``PENDING_APPROVAL`` → ``AWAITING_STEP_UP`` with its one challenge, atomically.

    Used when the post-signature policy pre-check asks for step-up.  Returns
    ``False`` (and changes nothing) if the authorization is not pending.  Traced as a
    ``step_up`` span with status ``pending``; it closes before the user reads the code.
    """
    from gateway.telemetry import stage

    with stage(
        "step_up",
        attributes={
            "authorization_id": authorization_id,
            "step_up_required": True,
            "channel": "simulated",
        },
    ) as span:
        entered = _enter_step_up(db_path, authorization_id, user_id, reason_codes)
        if entered:
            span.set_status(
                "pending", reason_code=reason_codes[0] if reason_codes else "STEP_UP_REQUIRED"
            )
        else:
            span.set_status("rejected", reason_code="NOT_PENDING")
        return entered


def _enter_step_up(
    db_path: str, authorization_id: str, user_id: str, reason_codes: tuple[str, ...]
) -> bool:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT authorization_id, user_id, status, envelope_hash, expires_at, "
            "conversation_id, conversation_revision FROM authorizations "
            "WHERE authorization_id = ? AND user_id = ?",
            (authorization_id, user_id),
        ).fetchone()
        if row is None or row["status"] != "PENDING_APPROVAL":
            conn.rollback()
            return False
        conn.execute(
            "UPDATE authorizations SET status = 'AWAITING_STEP_UP', status_reason = ? "
            "WHERE authorization_id = ? AND status = 'PENDING_APPROVAL'",
            (",".join(reason_codes) or None, authorization_id),
        )
        append_audit(
            conn,
            source="gateway",
            conversation_id=row["conversation_id"],
            user_id=user_id,
            step="gateway.authorization.awaiting_step_up",
            payload={"authorization_id": authorization_id, "reason_codes": list(reason_codes)},
        )
        issue_challenge_locked(
            conn,
            authorization_id=authorization_id,
            user_id=user_id,
            envelope_hash=row["envelope_hash"],
            expires_at=row["expires_at"],
            conversation_id=row["conversation_id"],
            conversation_revision=row["conversation_revision"],
            now=utcnow(),
        )
        conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _step_up_error_status(exc: StepUpError) -> str:
    """Telemetry status of a step-up refusal (wrong code / expiry are expected)."""
    if exc.code == "COMMIT_FAILED":
        return "error"
    if "EXPIRED" in exc.code or exc.status == "EXPIRED":
        return "expired"
    if exc.status == "BLOCKED":
        return "blocked"
    return "rejected"


# ── Service (routes call this) ─────────────────────────────────────


def _challenge_view(challenge: sqlite3.Row, authz_status: str, now: datetime) -> dict[str, Any]:
    status = challenge["status"]
    if status == "PENDING" and from_iso(challenge["expires_at"]) <= now:
        status = "EXPIRED"
    elif status == "PENDING" and authz_status != "AWAITING_STEP_UP":
        status = "INVALIDATED"
    return {
        "authorization_id": challenge["authorization_id"],
        "authorization_status": authz_status,
        "challenge_id": challenge["challenge_id"],
        "status": status,
        "expires_at": challenge["expires_at"],
        "attempts_remaining": max(
            0, int(challenge["max_attempts"]) - int(challenge["failed_attempts"])
        ),
        "deliveries": int(challenge["deliveries"]),
        "channel": SIMULATION_LABEL,
    }


class StepUpService:
    """Step-up submission, status, resend, cancel and the operator channel."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return connect(self._db_path)

    def _owned(self, conn: sqlite3.Connection, authz_id: str, user_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT authorization_id, user_id, operation, status, status_reason, envelope_hash, "
            "expires_at, conversation_id FROM authorizations "
            "WHERE authorization_id = ? AND user_id = ?",
            (authz_id, user_id),
        ).fetchone()
        if row is None:
            raise StepUpError("AUTHORIZATION_NOT_FOUND")
        return row

    # ── User routes ──────────────────────────────────────────────

    def submit(
        self,
        *,
        user_id: str,
        authz_id: str,
        code: str,
        expected_envelope_hash: str | None = None,
    ) -> dict[str, Any]:
        """Verify *code* and commit in one transaction (see the module docstring).

        Traced as a ``step_up`` span (nested ``execution``/``policy``/``receipt``); only
        stable codes are recorded, never the code the user typed.
        """
        from gateway.telemetry import stage

        conn = self._conn()
        try:
            authz = self._owned(conn, authz_id, user_id)
        finally:
            conn.close()
        with stage(
            "step_up",
            user_id=user_id,
            conversation_id=authz["conversation_id"],
            attributes={
                "authorization_id": authz_id,
                "step_up_required": True,
                "channel": "simulated",
            },
        ) as span:
            try:
                result = self._submit(authz, user_id, authz_id, code, expected_envelope_hash)
            except StepUpError as exc:
                span.set_status(_step_up_error_status(exc), reason_code=exc.code)
                raise
            span.set_attributes({"approval_state": result.get("status")})
            if result.get("status") == "BLOCKED":
                codes = result.get("reason_codes") or []
                span.set_status("blocked", reason_code=codes[0] if codes else "POLICY_BLOCK")
            return result

    def _submit(
        self,
        authz: sqlite3.Row,
        user_id: str,
        authz_id: str,
        code: str,
        expected_envelope_hash: str | None,
    ) -> dict[str, Any]:
        if authz["operation"] != "execute_bundle":
            raise StepUpError("OPERATION_MISMATCH")
        if expected_envelope_hash is not None and not hmac.compare_digest(
            str(expected_envelope_hash), authz["envelope_hash"]
        ):
            raise StepUpError("ENVELOPE_HASH_MISMATCH")

        proof = CodeProof(code)
        try:
            receipt = commit(
                self._db_path,
                {"authorization_id": authz_id, "user_id": user_id},
                utcnow(),
                step_up_proof=proof,
            )
        except ExecutionRefused as exc:
            if exc.status == "BLOCKED" and exc.code in _BLOCKED_NOW:
                return {
                    "authorization_id": authz_id,
                    "status": "BLOCKED",
                    "reason": ",".join(exc.reason_codes),
                    "reason_codes": list(exc.reason_codes),
                }
            remaining = self._attempts_remaining(authz_id)
            if exc.code == "NOT_PENDING":
                raise StepUpError(f"NOT_PENDING: {exc.status}", status=exc.status) from None
            if exc.code == "STEP_UP_NOT_REQUIRED":
                raise StepUpError("STEP_UP_NOT_REQUIRED", status=exc.status) from None
            raise StepUpError(
                exc.code, exc.reason_codes, status=exc.status, attempts_remaining=remaining
            ) from None
        except ExecutionFailed:
            if proof.accepted:
                self._invalidate_after_failure(authz_id, user_id)
            raise StepUpError("COMMIT_FAILED") from None
        return {
            "authorization_id": authz_id,
            "status": receipt.status,
            "receipt_id": receipt.receipt_id,
            "receipt": dict(receipt.details),
            "replayed": receipt.replayed,
        }

    def _attempts_remaining(self, authz_id: str) -> int | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT max_attempts, failed_attempts FROM step_up_challenges "
                "WHERE authorization_id = ?",
                (authz_id,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else max(0, int(row[0]) - int(row[1]))

    def _invalidate_after_failure(self, authz_id: str, user_id: str) -> None:
        """The commit rolled back after a valid code: make the code unusable."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            close_for_authorization_locked(
                conn, authz_id, "INVALIDATED", ("COMMIT_FAILED",), utcnow()
            )
            conn.commit()
        except Exception:  # best effort; the authorization still expires within 120 s
            if conn.in_transaction:
                conn.rollback()
            logger.warning("could not invalidate the step-up challenge after a failed commit")
        finally:
            conn.close()

    def status(self, *, user_id: str, authz_id: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            authz = self._owned(conn, authz_id, user_id)
            challenge = conn.execute(
                "SELECT * FROM step_up_challenges WHERE authorization_id = ?", (authz_id,)
            ).fetchone()
        finally:
            conn.close()
        if challenge is None:
            raise StepUpError("STEP_UP_NOT_ISSUED", status=authz["status"])
        return _challenge_view(challenge, authz["status"], utcnow())

    def resend(self, *, user_id: str, authz_id: str) -> dict[str, Any]:
        """Ask the simulated channel to re-deliver the same code; expiry is unchanged."""
        now = utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authz = self._owned(conn, authz_id, user_id)
            challenge = self._usable_challenge(conn, authz, now)
            append_audit(
                conn,
                source="gateway",
                conversation_id=challenge["conversation_id"],
                user_id=user_id,
                step="gateway.stepup.resend_requested",
                payload={
                    "authorization_id": authz_id,
                    "challenge_id": challenge["challenge_id"],
                    "expires_at": challenge["expires_at"],
                },
            )
            conn.commit()
            return _challenge_view(challenge, authz["status"], now)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _usable_challenge(
        conn: sqlite3.Connection, authz: sqlite3.Row, now: datetime
    ) -> sqlite3.Row:
        challenge = conn.execute(
            "SELECT * FROM step_up_challenges WHERE authorization_id = ?",
            (authz["authorization_id"],),
        ).fetchone()
        if challenge is None:
            raise StepUpError("STEP_UP_NOT_ISSUED", status=authz["status"])
        if challenge["status"] != "PENDING" or authz["status"] != "AWAITING_STEP_UP":
            raise StepUpError("STEP_UP_NOT_ACTIVE", status=authz["status"])
        if from_iso(challenge["expires_at"]) <= now:
            raise StepUpError("STEP_UP_EXPIRED", status=authz["status"])
        return challenge

    def cancel(self, *, user_id: str, authz_id: str) -> dict[str, Any]:
        """User cancel: PENDING_APPROVAL / AWAITING_STEP_UP → CANCELLED (closes the challenge)."""
        now = utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authz = self._owned(conn, authz_id, user_id)
            status = authz["status"]
            if status not in ("PENDING_APPROVAL", "AWAITING_STEP_UP"):
                raise StepUpError(f"NOT_PENDING: {status}", status=status)
            conn.execute(
                "UPDATE authorizations SET status = 'CANCELLED', status_reason = 'USER_CANCELLED' "
                "WHERE authorization_id = ? AND status = ?",
                (authz_id, status),
            )
            close_for_authorization_locked(conn, authz_id, "CANCELLED", ("USER_CANCELLED",), now)
            append_audit(
                conn,
                source="gateway",
                conversation_id=authz["conversation_id"],
                user_id=user_id,
                step="gateway.authorization.cancelled",
                payload={
                    "authorization_id": authz_id,
                    "from_status": status,
                    "reason_codes": ["USER_CANCELLED"],
                },
            )
            conn.commit()
            return {
                "authorization_id": authz_id,
                "status": "CANCELLED",
                "reason_codes": ["USER_CANCELLED"],
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    # ── Operator console (simulated delivery) ─────────────────────

    def operator_pending(self) -> list[dict[str, Any]]:
        """Usable challenges (no codes): what the simulated channel could deliver."""
        now = utcnow()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT c.*, a.status AS authz_status FROM step_up_challenges c "
                "JOIN authorizations a ON a.authorization_id = c.authorization_id "
                "WHERE c.status = 'PENDING' AND a.status = 'AWAITING_STEP_UP' "
                "ORDER BY c.issued_at"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            if from_iso(row["expires_at"]) > now:
                view = _challenge_view(row, row["authz_status"], now)
                view["user_id"] = row["user_id"]
                out.append(view)
        return out

    def operator_deliver(self, authz_id: str) -> dict[str, Any]:
        """Simulate delivering (or re-delivering) the code to the user's phone.

        Returns the code to the operator console only.  Never extends expiry;
        audited without the code.
        """
        now = utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authz = conn.execute(
                "SELECT authorization_id, user_id, status, envelope_hash FROM authorizations "
                "WHERE authorization_id = ?",
                (authz_id,),
            ).fetchone()
            if authz is None:
                raise StepUpError("AUTHORIZATION_NOT_FOUND")
            challenge = self._usable_challenge(conn, authz, now)
            code = _derive_code(challenge["challenge_id"], challenge["salt"])
            expected = _verifier(
                user_id=authz["user_id"],
                authorization_id=authz_id,
                envelope_hash=authz["envelope_hash"],
                challenge_id=challenge["challenge_id"],
                salt=challenge["salt"],
                code=code,
            )
            if not hmac.compare_digest(expected, challenge["verifier"]):
                # Issued under another secret (e.g. a restart with a per-process secret).
                raise StepUpError("STEP_UP_CODE_UNAVAILABLE", status=authz["status"])
            conn.execute(
                "UPDATE step_up_challenges SET deliveries = deliveries + 1, "
                "last_delivered_at = ? WHERE challenge_id = ?",
                (to_iso(now), challenge["challenge_id"]),
            )
            append_audit(
                conn,
                source="gateway",
                conversation_id=challenge["conversation_id"],
                user_id=authz["user_id"],
                step="gateway.stepup.delivered",
                payload={
                    "authorization_id": authz_id,
                    "challenge_id": challenge["challenge_id"],
                    "deliveries": int(challenge["deliveries"]) + 1,
                    "channel": "simulated-operator-console",
                },
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "simulation": SIMULATION_LABEL,
            "authorization_id": authz_id,
            "user_id": authz["user_id"],
            "challenge_id": challenge["challenge_id"],
            "code": code,
            "expires_at": challenge["expires_at"],
            "attempts_remaining": max(
                0, int(challenge["max_attempts"]) - int(challenge["failed_attempts"])
            ),
            "deliveries": int(challenge["deliveries"]) + 1,
        }
