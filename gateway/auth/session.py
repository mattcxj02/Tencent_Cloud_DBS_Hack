"""User session dependency — real WebAuthn session validation.

Reads the ``dcta_session`` cookie, validates it against the server-side
session store, and returns a :class:`UserPrincipal` on success.  On any
doubt (missing cookie, invalid token, expired or idle-timed-out
session) the dependency raises 401 — it **always fails closed**.

Tests override this dependency via ``app.dependency_overrides`` only;
there is no runtime bypass flag, header, or query parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

__all__ = ["UserPrincipal", "require_user_session"]


@dataclass(frozen=True)
class UserPrincipal:
    """The authenticated user derived from a WebAuthn session."""

    user_id: str


def require_user_session(request: Request) -> UserPrincipal:
    """Validate the session cookie and return the authenticated user.

    Raises 401 if the cookie is missing, the session is invalid, expired,
    or idle-timed-out.  Tests override via
    ``app.dependency_overrides[require_user_session]``.
    """
    from gateway.identity import SESSION_COOKIE_NAME, get_identity_store

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing session cookie",
        )

    store = get_identity_store()
    user_id = store.validate_session(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired session",
        )

    return UserPrincipal(user_id=user_id)
