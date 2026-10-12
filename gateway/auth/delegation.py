"""Service-to-gateway delegation tokens (interim, until T-05 adds user sessions).

The gateway issues short-lived HMAC-SHA256 tokens binding::

    {user_id, conversation_id (optional), scopes, issued_at, expires_at, token_id}

with a gateway-only secret.  Verify with :func:`hmac.compare_digest`, reject
expired/tampered/wrong-scope tokens.  The caller can never choose the user by
header or query param; the user comes only from the verified token.

A FastAPI dependency :func:`require_principal` returns a :class:`Principal`.
Tests mint tokens with the real issuer; there is no runtime bypass flag or
header.  T-03 will hand these tokens to the agent; T-05 adds browser sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status

from shared.settings import load_gateway_settings

__all__ = [
    "Principal",
    "DelegationIssuer",
    "get_delegation_issuer",
    "require_principal",
    "TOKEN_PREFIX",
]

logger = logging.getLogger("dcta.gateway.delegation")

#: Prefix on all delegation tokens so they are easy to identify.
TOKEN_PREFIX = "dcta_"

#: Default token lifetime (seconds) — short-lived.
DEFAULT_TTL_SECONDS = 300  # 5 minutes

#: Minimum configured secret length; shorter or placeholder values are rejected.
MIN_SECRET_LENGTH = 32

#: Maximum query length for payee search.
MAX_QUERY_LENGTH = 200


@dataclass(frozen=True)
class Principal:
    """The authenticated principal derived from a verified delegation token."""

    user_id: str
    scopes: frozenset[str]
    conversation_id: str | None = None
    token_id: str = field(default="")
    run_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class DelegationIssuer:
    """Issue and verify HMAC-SHA256 delegation tokens.

    The secret comes from ``GATEWAY_DELEGATION_SECRET`` in settings.  If
    unset, a random per-process secret is generated and a warning is logged.
    Never a hard-coded default.
    """

    def __init__(
        self, secret: str | None = None, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        if secret:
            if len(secret) < MIN_SECRET_LENGTH or secret.startswith("replace-with"):
                raise ValueError(
                    f"GATEWAY_DELEGATION_SECRET must be a random value of at least "
                    f"{MIN_SECRET_LENGTH} characters (e.g. `python -c 'import secrets; "
                    f"print(secrets.token_urlsafe(48))'`)"
                )
            self._secret = secret.encode("utf-8")
            self._ephemeral = False
        else:
            self._secret = secrets.token_bytes(32)
            self._ephemeral = True
            logger.warning(
                "GATEWAY_DELEGATION_SECRET is not set; using a random "
                "per-process secret. Tokens will not survive restarts."
            )
        self._ttl_seconds = ttl_seconds

    @property
    def ephemeral(self) -> bool:
        """True if using a random per-process secret (not configured)."""
        return self._ephemeral

    def issue_token(
        self,
        user_id: str,
        scopes: list[str],
        *,
        conversation_id: str | None = None,
        ttl_seconds: int | None = None,
        run_id: str | None = None,
    ) -> str:
        """Issue a delegation token for *user_id* with *scopes*.

        Returns a URL-safe string token.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        now = datetime.now(tz=UTC)
        issued_at = int(now.timestamp())
        expires_at = issued_at + ttl
        token_id = uuid.uuid4().hex

        payload: dict[str, Any] = {
            "user_id": user_id,
            "scopes": sorted(s for s in scopes),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "token_id": token_id,
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id

        if run_id is not None:
            payload["run_id"] = run_id
        return self._encode(payload)

    def verify_token(self, token: str) -> Principal:
        """Verify a delegation token and return a :class:`Principal`.

        Raises :class:`HTTPException` (401) if the token is tampered,
        expired, or malformed.
        """
        payload = self._decode(token)

        # Check expiry.
        now_ts = int(datetime.now(tz=UTC).timestamp())
        if now_ts >= payload["expires_at"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="delegation token expired",
            )

        user_id = payload["user_id"]
        scopes = frozenset(payload.get("scopes", []))
        token_id = payload.get("token_id", "")
        conversation_id = payload.get("conversation_id")

        return Principal(
            user_id=user_id,
            scopes=scopes,
            conversation_id=conversation_id,
            token_id=token_id,
            run_id=payload.get("run_id"),
        )

    # ── internal encoding/decoding ──────────────────────────────────

    def _encode(self, payload: dict[str, Any]) -> str:
        """Encode payload as base64(json) + HMAC signature."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self._secret, body, hashlib.sha256).digest()
        encoded_body = base64.urlsafe_b64encode(body).decode("ascii")
        encoded_sig = base64.urlsafe_b64encode(sig).decode("ascii")
        return f"{TOKEN_PREFIX}{encoded_body}.{encoded_sig}"

    def _decode(self, token: str) -> dict[str, Any]:
        """Decode and verify token.  Raises HTTPException on failure."""
        if not token.startswith(TOKEN_PREFIX):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token format",
            )
        token = token[len(TOKEN_PREFIX) :]

        parts = token.split(".")
        if len(parts) != 2:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token format",
            )
        encoded_body, encoded_sig = parts

        try:
            body = base64.urlsafe_b64decode(encoded_body)
            sig = base64.urlsafe_b64decode(encoded_sig)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token encoding",
            ) from None

        # Verify HMAC in constant time.
        expected_sig = hmac.new(self._secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, sig):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="token signature verification failed",
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token payload",
            ) from None

        return payload


# ── Module-level singleton ──────────────────────────────────────────

_issuer: DelegationIssuer | None = None


def get_delegation_issuer() -> DelegationIssuer:
    """Get the module-level :class:`DelegationIssuer` singleton.

    Created lazily from gateway settings on first call.
    """
    global _issuer  # noqa: PLW0603
    if _issuer is None:
        settings = load_gateway_settings()
        _issuer = DelegationIssuer(secret=settings.gateway_delegation_secret)
    return _issuer


def _set_delegation_issuer(issuer: DelegationIssuer | None) -> None:
    """Test override: inject a specific issuer (or reset to None)."""
    global _issuer  # noqa: PLW0603
    _issuer = issuer


def _extract_bearer_token(request: Request) -> str:
    """Extract the Bearer token from the Authorization header.

    The user can never be overridden by any other header or query param.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization header",
        )
    return auth_header[len("Bearer ") :].strip()


def require_principal(scope: str):
    """FastAPI dependency returning a :class:`Principal` with *scope*.

    Usage::

        @app.get("/internal/bank/accounts", dependencies=[Depends(require_principal("bank:read"))])
        async def get_accounts(principal: Principal = Depends(require_principal("bank:read"))):
            ...

    The caller can never choose the user by header or query param.
    """

    def _dependency(request: Request) -> Principal:
        token = _extract_bearer_token(request)
        issuer = get_delegation_issuer()
        principal = issuer.verify_token(token)
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required scope: {scope}",
            )
        if scope != "audit:append_agent":
            from gateway.drafting_work import check_run
            from shared.deadline import DeadlineExpired

            try:
                check_run(principal.run_id)
            except DeadlineExpired:
                raise HTTPException(status_code=409, detail="DRAFT_WORK_INACTIVE") from None
        return principal

    return _dependency
