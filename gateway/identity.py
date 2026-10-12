"""Gateway-owned identity store: enrollment codes, WebAuthn credentials,
server-side sessions, challenge store, and in-memory rate limiting.

All data is stored in the gateway-owned SQLite database.  Enrollment codes
are random ≥128 bits stored only as a SHA-256 hash.  Session IDs are random
256-bit values stored only as a hash.  Challenges are stored hashed and
consumed once.

This module is imported only by gateway code.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from shared.timeutil import now_utc, to_iso

__all__ = [
    "IdentityStore",
    "ENROLLMENT_CODE_BITS",
    "ENROLLMENT_EXPIRY_MINUTES",
    "SESSION_BITS",
    "SESSION_DEFAULT_TTL_SECONDS",
    "SESSION_IDLE_TIMEOUT_SECONDS",
    "CHALLENGE_BITS",
    "CHALLENGE_EXPIRY_SECONDS",
    "CHALLENGE_NOT_FOUND",
    "RATE_LIMIT_MAX",
    "RATE_LIMIT_WINDOW_SECONDS",
    "SESSION_COOKIE_NAME",
]

#: Enrollment code entropy (≥128 bits).
ENROLLMENT_CODE_BITS = 128

#: Enrollment code expiry (15 minutes).
ENROLLMENT_EXPIRY_MINUTES = 15

#: Session ID entropy (256 bits).
SESSION_BITS = 256

#: Default session absolute TTL (8 hours).
SESSION_DEFAULT_TTL_SECONDS = 8 * 3600

#: Idle timeout (30 minutes).
SESSION_IDLE_TIMEOUT_SECONDS = 1800

#: Challenge entropy.
CHALLENGE_BITS = 256

#: Challenge expiry (2 minutes — enough for a browser ceremony).
CHALLENGE_EXPIRY_SECONDS = 120

#: Rate limit: max login challenge requests per window.
RATE_LIMIT_MAX = 10

#: Rate limit window (60 seconds).
RATE_LIMIT_WINDOW_SECONDS = 60

#: Sentinel returned by ``consume_challenge`` when the challenge is not
#: found, expired, or already consumed — distinct from ``None`` which is
#: a valid ``user_id`` for login (non-user-bound) challenges.
CHALLENGE_NOT_FOUND = object()

#: Cookie name for the session.
SESSION_COOKIE_NAME = "dcta_session"


def _hash_hex(value: str | bytes) -> str:
    """SHA-256 hex of a string or bytes value."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class IdentityStore:
    """Read/write access to identity tables.

    Instantiated by gateway code with a DB path.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._rate_buckets: dict[str, list[datetime]] = defaultdict(list)

    def _conn(self) -> sqlite3.Connection:
        from ledger.schema import connect

        return connect(self._db_path)

    # ── Enrollment codes ───────────────────────────────────────────

    def create_enrollment_code(self, user_id: str) -> str:
        """Generate a one-time enrollment code for *user_id*.

        The code is random ≥128 bits, stored only as a hash, with a
        15-minute expiry and single-use semantics.
        """
        code = secrets.token_urlsafe(ENROLLMENT_CODE_BITS // 8)
        code_hash = _hash_hex(code)
        now = now_utc()
        expires_at = to_iso(now + timedelta(minutes=ENROLLMENT_EXPIRY_MINUTES))

        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO enrollment_codes (code_hash, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (code_hash, user_id, expires_at, to_iso(now)),
            )
            conn.commit()
        finally:
            conn.close()

        return code

    def begin_enrollment(self, code: str, challenge: bytes) -> str:
        """Reserve one of five begins without consuming the 15-minute code."""
        code_hash = _hash_hex(code)
        now = now_utc()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._enrollment_code(conn, code_hash, now)
            attempts = conn.execute(
                "SELECT COUNT(*) FROM enrollment_challenges WHERE code_hash = ?", (code_hash,)
            ).fetchone()[0]
            if attempts >= 5:
                raise ValueError("enrollment begin limit reached")
            from shared.timeutil import from_iso

            expires = min(
                from_iso(row["expires_at"]), now + timedelta(seconds=CHALLENGE_EXPIRY_SECONDS)
            )
            conn.execute(
                "INSERT INTO auth_challenges "
                "(challenge_hash, user_id, purpose, created_at, expires_at) "
                "VALUES (?, ?, 'registration', ?, ?)",
                (_hash_hex(challenge), row["user_id"], to_iso(now), to_iso(expires)),
            )
            conn.execute(
                "INSERT INTO enrollment_challenges (challenge_hash, code_hash) VALUES (?, ?)",
                (_hash_hex(challenge), code_hash),
            )
            conn.commit()
            return row["user_id"]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _enrollment_code(conn: sqlite3.Connection, code_hash: str, now: datetime) -> sqlite3.Row:
        from shared.timeutil import from_iso

        row = conn.execute(
            "SELECT user_id, expires_at, used_at FROM enrollment_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("invalid enrollment code")
        if row["used_at"] is not None:
            raise ValueError("enrollment code already used")
        if from_iso(row["expires_at"]) <= now:
            raise ValueError("enrollment code expired")
        return row

    def complete_enrollment(
        self,
        challenge: bytes,
        *,
        credential_id: str,
        public_key: bytes,
        sign_count: int,
        transports: list[str],
        backup_eligible: bool,
        backup_state: bool,
    ) -> str:
        """Register a verified credential, consume its code and invalidate siblings atomically."""
        from shared.timeutil import from_iso

        now = now_utc()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT c.user_id, c.expires_at, c.consumed_at, e.code_hash "
                "FROM auth_challenges c JOIN enrollment_challenges e "
                "ON e.challenge_hash = c.challenge_hash "
                "WHERE c.challenge_hash = ? AND c.purpose = 'registration'",
                (_hash_hex(challenge),),
            ).fetchone()
            if row is None or row["consumed_at"] is not None or from_iso(row["expires_at"]) <= now:
                raise ValueError("challenge not found, expired, or already used")
            code = self._enrollment_code(conn, row["code_hash"], now)
            if code["user_id"] != row["user_id"]:
                raise ValueError("enrollment binding mismatch")
            conn.execute(
                "INSERT INTO webauthn_credentials "
                "(credential_id, user_id, public_key, sign_count, transports, "
                "backup_eligible, backup_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (credential_id, code["user_id"], public_key, sign_count, ",".join(transports),
                 int(backup_eligible), int(backup_state), to_iso(now)),
            )
            conn.execute(
                "UPDATE enrollment_codes SET used_at = ? WHERE code_hash = ?",
                (to_iso(now), row["code_hash"]),
            )
            conn.execute(
                "UPDATE auth_challenges SET consumed_at = ? WHERE challenge_hash IN "
                "(SELECT challenge_hash FROM enrollment_challenges WHERE code_hash = ?)",
                (to_iso(now), row["code_hash"]),
            )
            conn.commit()
            return code["user_id"]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── WebAuthn credentials ───────────────────────────────────────

    def store_credential(
        self,
        *,
        credential_id: str,
        user_id: str,
        public_key: bytes,
        sign_count: int,
        transports: list[str] | None,
        backup_eligible: bool,
        backup_state: bool,
    ) -> None:
        """Store a verified WebAuthn credential."""
        now = to_iso(now_utc())
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO webauthn_credentials
                    (credential_id, user_id, public_key, sign_count,
                     transports, backup_eligible, backup_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    user_id,
                    public_key,
                    sign_count,
                    ",".join(transports) if transports else "",
                    1 if backup_eligible else 0,
                    1 if backup_state else 0,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        """Return a credential row by ID."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT credential_id, user_id, public_key, sign_count,
                       transports, backup_eligible, backup_state, created_at
                FROM webauthn_credentials
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
            if row is None:
                return None
            return _cred_row_to_dict(row)
        finally:
            conn.close()

    def get_user_credentials(self, user_id: str) -> list[dict[str, Any]]:
        """Return all credentials for a user."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT credential_id, user_id, public_key, sign_count,
                       transports, backup_eligible, backup_state, created_at
                FROM webauthn_credentials
                WHERE user_id = ?
                ORDER BY created_at
                """,
                (user_id,),
            ).fetchall()
            return [_cred_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def update_sign_count(self, credential_id: str, new_count: int) -> None:
        """Update the stored sign count for a credential."""
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count = ? WHERE credential_id = ?",
                (new_count, credential_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ── Sessions ───────────────────────────────────────────────────

    def create_session(
        self,
        user_id: str,
        *,
        ttl_seconds: int = SESSION_DEFAULT_TTL_SECONDS,
        idle_timeout: int = SESSION_IDLE_TIMEOUT_SECONDS,
    ) -> str:
        """Create a server-side session and return the raw token.

        The token is random 256 bits; only its hash is stored.
        """
        token = secrets.token_urlsafe(SESSION_BITS // 8)
        token_hash = _hash_hex(token)
        now = now_utc()
        created_at = to_iso(now)
        expires_at = to_iso(now + timedelta(seconds=ttl_seconds))

        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO user_sessions
                    (session_hash, user_id, created_at, expires_at,
                     last_activity, idle_timeout_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created_at, expires_at, created_at, idle_timeout),
            )
            conn.commit()
        finally:
            conn.close()

        return token

    def validate_session(self, token: str) -> str | None:
        """Validate a session token and return the user_id, or None.

        Checks: hash matches, not expired, idle timeout not exceeded.
        Updates ``last_activity`` on success.
        """
        token_hash = _hash_hex(token)
        now = now_utc()

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT session_hash, user_id, created_at, expires_at,
                       last_activity, idle_timeout_seconds
                FROM user_sessions
                WHERE session_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None

            from shared.timeutil import from_iso

            # Check absolute expiry.
            if from_iso(row["expires_at"]) <= now:
                conn.rollback()
                return None

            # Check idle timeout.
            last_activity = from_iso(row["last_activity"])
            idle_seconds = int(row["idle_timeout_seconds"])
            if (now - last_activity).total_seconds() > idle_seconds:
                conn.rollback()
                return None

            # Update last_activity.
            conn.execute(
                "UPDATE user_sessions SET last_activity = ? WHERE session_hash = ?",
                (to_iso(now), token_hash),
            )
            conn.commit()
            return row["user_id"]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def destroy_session(self, token: str) -> None:
        """Delete a session."""
        token_hash = _hash_hex(token)
        conn = self._conn()
        try:
            conn.execute("DELETE FROM user_sessions WHERE session_hash = ?", (token_hash,))
            conn.commit()
        finally:
            conn.close()

    # ── Challenges ─────────────────────────────────────────────────

    def store_challenge(
        self,
        challenge: bytes,
        *,
        purpose: str,
        user_id: str | None = None,
        expiry_seconds: int = CHALLENGE_EXPIRY_SECONDS,
    ) -> None:
        """Store a challenge hash with short expiry and single-use semantics."""
        challenge_hash = _hash_hex(challenge)
        now = now_utc()
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO auth_challenges
                    (challenge_hash, user_id, purpose, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    challenge_hash,
                    user_id,
                    purpose,
                    to_iso(now),
                    to_iso(now + timedelta(seconds=expiry_seconds)),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def consume_challenge(self, challenge: bytes, purpose: str) -> str | Any | None:
        """Consume a challenge and return the bound user_id, or :data:`CHALLENGE_NOT_FOUND`.

        Returns the sentinel :data:`CHALLENGE_NOT_FOUND` (distinct from ``None``)
        when the challenge is not found, expired, or already consumed —
        so callers can distinguish failure from a valid ``user_id=None``
        (login challenges are not user-bound).

        On success, marks as consumed and returns the stored ``user_id``
        (which may be ``None``).
        """
        challenge_hash = _hash_hex(challenge)
        now = now_utc()

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT challenge_hash, user_id, purpose, created_at,
                       expires_at, consumed_at
                FROM auth_challenges
                WHERE challenge_hash = ? AND purpose = ?
                """,
                (challenge_hash, purpose),
            ).fetchone()
            if row is None:
                conn.rollback()
                return CHALLENGE_NOT_FOUND
            if row["consumed_at"] is not None:
                conn.rollback()
                return CHALLENGE_NOT_FOUND
            from shared.timeutil import from_iso

            if from_iso(row["expires_at"]) <= now:
                conn.rollback()
                return CHALLENGE_NOT_FOUND
            conn.execute(
                "UPDATE auth_challenges SET consumed_at = ? WHERE challenge_hash = ?",
                (to_iso(now), challenge_hash),
            )
            conn.commit()
            return row["user_id"]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Rate limiting (in-memory, per IP) ──────────────────────────

    def check_rate_limit(self, key: str) -> bool:
        """Return True if the request is within the rate limit.

        Records the current timestamp and prunes old entries.
        """
        now = now_utc()
        cutoff = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        bucket = self._rate_buckets[key]
        # Prune old entries.
        self._rate_buckets[key] = [ts for ts in bucket if ts > cutoff]
        bucket = self._rate_buckets[key]
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def _cred_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "credential_id": row["credential_id"],
        "user_id": row["user_id"],
        "public_key": bytes(row["public_key"]),
        "sign_count": int(row["sign_count"]),
        "transports": [t for t in (row["transports"] or "").split(",") if t],
        "backup_eligible": bool(row["backup_eligible"]),
        "backup_state": bool(row["backup_state"]),
        "created_at": row["created_at"],
    }


# ── Module-level singleton ─────────────────────────────────────────

_store: IdentityStore | None = None


def get_identity_store() -> IdentityStore:
    """Get the module-level IdentityStore singleton."""
    global _store
    if _store is None:
        from shared.settings import load_gateway_settings

        _store = IdentityStore(load_gateway_settings().db_path)
    return _store


def _set_identity_store(store: IdentityStore | None) -> None:
    """Test override: inject a store with a temp DB path."""
    global _store
    _store = store
