"""WebAuthn enrollment, login, and logout routes.

Enrollment is protected by a one-time enrollment code (bootstrap).  The
caller can never choose the user ID — the credential is registered to the
code's bound user.  Registration and login challenges are stored server-side
with short expiry and consumed once.  Sessions are server-side rows in an
HttpOnly, SameSite=Strict cookie (Secure when origin is https).

Mutating routes require the ``Origin`` header to equal the configured origin
(CSRF protection).  CORS is restricted to that origin.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.identity import (
    CHALLENGE_NOT_FOUND,
    SESSION_COOKIE_NAME,
    get_identity_store,
)
from shared.settings import load_gateway_settings

router = APIRouter(tags=["auth"])


class _AuthBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollBeginRequest(_AuthBase):
    code: str = Field(..., min_length=1)


class EnrollVerifyRequest(_AuthBase):
    """The browser's registration credential response."""

    credential: dict[str, Any]


class LoginVerifyRequest(_AuthBase):
    """The browser's authentication credential response."""

    assertion: dict[str, Any]


class AuthResponse(_AuthBase):
    user_id: str


# ── Helpers ─────────────────────────────────────────────────────────


def _get_settings():
    return load_gateway_settings()


def _check_origin(request: Request) -> None:
    """Reject mutating requests without the correct Origin header."""
    settings = _get_settings()
    origin = request.headers.get("origin", "")
    if origin != settings.rp_origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="origin not allowed",
        )


# ── Enrollment ──────────────────────────────────────────────────────


@router.post("/api/auth/enroll/begin")
async def enroll_begin(
    body: EnrollBeginRequest,
    request: Request,
) -> dict[str, Any]:
    """Begin enrollment: reserve a code-bound challenge and return options."""
    _check_origin(request)
    store = get_identity_store()
    challenge = secrets.token_bytes(32)
    try:
        user_id = store.begin_enrollment(body.code, challenge)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None

    settings = _get_settings()

    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name="DCTA",
        user_name=user_id,
        user_id=user_id.encode("utf-8"),
        challenge=challenge,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable credential (passkey): login sends no allowCredentials.
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )

    return json.loads(options_to_json(options))


@router.post("/api/auth/enroll/verify")
async def enroll_verify(
    body: EnrollVerifyRequest,
    request: Request,
) -> dict[str, str]:
    """Verify the registration response and store the credential."""
    _check_origin(request)
    store = get_identity_store()
    settings = _get_settings()

    # Extract challenge from the credential's clientDataJSON.
    try:
        client_data = _extract_client_data(body.credential)
        challenge_b64 = client_data["challenge"]
        challenge = _b64url_to_bytes(challenge_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"malformed credential: {exc}",
        ) from None

    try:
        verified = verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.rp_origin,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"registration verification failed: {exc}",
        ) from None

    # Store the credential.
    # Transports come from the browser's credential response, not from
    # VerifiedRegistration (which doesn't carry them).
    transports_raw = body.credential.get("response", {}).get("transports", [])
    try:
        user_id = store.complete_enrollment(
            challenge,
            credential_id=bytes_to_base64url(verified.credential_id),
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=transports_raw if isinstance(transports_raw, list) else [],
            backup_eligible=verified.credential_backed_up,
            backup_state=verified.credential_backed_up,
        )
    except (ValueError, sqlite3.IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="enrollment expired, used, or credential already registered",
        ) from None

    return {"user_id": user_id, "status": "enrolled"}


# ── Login ──────────────────────────────────────────────────────────


@router.post("/api/auth/login/begin")
async def login_begin(
    request: Request,
) -> dict[str, Any]:
    """Begin login: return authentication options.

    Rate limited per IP.  Does not require a session.
    """
    store = get_identity_store()
    settings = _get_settings()

    # Rate limit per IP.
    client_ip = request.client.host if request.client else "unknown"
    if not store.check_rate_limit(f"login:{client_ip}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
        )

    import secrets

    challenge = secrets.token_bytes(32)

    options = generate_authentication_options(
        rp_id=settings.rp_id,
        challenge=challenge,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    store.store_challenge(challenge, purpose="authentication")

    return json.loads(options_to_json(options))


@router.post("/api/auth/login/verify")
async def login_verify(
    body: LoginVerifyRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Verify the authentication response and create a session."""
    _check_origin(request)
    store = get_identity_store()
    settings = _get_settings()

    # Extract challenge from the assertion's clientDataJSON.
    try:
        client_data = _extract_client_data(body.assertion)
        challenge_b64 = client_data["challenge"]
        challenge = _b64url_to_bytes(challenge_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"malformed assertion: {exc}",
        ) from None

    # Consume the stored challenge (single-use).  Login challenges are
    # not user-bound (user_id=None when stored); the credential lookup
    # below provides the user binding.  We need a distinct sentinel to
    # tell "not found" from "found with user_id=None".
    challenge_result = store.consume_challenge(challenge, purpose="authentication")
    if challenge_result is CHALLENGE_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="challenge not found, expired, or already used",
        )

    # Extract credential ID from the assertion to look up the credential.
    cred_id_b64 = body.assertion.get("id", "")
    if not cred_id_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing credential id",
        )

    cred = store.get_credential(cred_id_b64)
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credential not found",
        )

    try:
        verified = verify_authentication_response(
            credential=body.assertion,
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.rp_origin,
            credential_public_key=cred["public_key"],
            credential_current_sign_count=cred["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"authentication verification failed: {exc}",
        ) from None

    # Update sign count.
    store.update_sign_count(cred["credential_id"], verified.new_sign_count)

    # Create session.
    token = store.create_session(cred["user_id"])

    # Set cookie.
    secure = settings.rp_origin.startswith("https://")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )

    return {"user_id": cred["user_id"]}


# ── Current user ───────────────────────────────────────────────────


@router.get("/api/auth/me")
def me(user: Annotated[UserPrincipal, Depends(require_user_session)]) -> dict[str, Any]:
    """The session user: ``{user_id, display_name, credentials: [{credential_id,
    created_at}]}``. Public key, sign count and transports are not returned."""
    store = get_identity_store()
    conn = store._conn()
    try:
        row = conn.execute(
            "SELECT display_name FROM users WHERE user_id = ?", (user.user_id,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "user_id": user.user_id,
        "display_name": row["display_name"] if row is not None else user.user_id,
        "credentials": [
            {"credential_id": c["credential_id"], "created_at": c["created_at"]}
            for c in store.get_user_credentials(user.user_id)
        ],
    }


# ── Logout ─────────────────────────────────────────────────────────


@router.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> dict[str, str]:
    """Destroy the session and clear the cookie."""
    _check_origin(request)
    store = get_identity_store()
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token:
        store.destroy_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}


# ── Utility ────────────────────────────────────────────────────────


def _extract_client_data(credential: dict[str, Any]) -> dict[str, Any]:
    """Extract and parse clientDataJSON from a credential dict."""
    import base64

    # Registration: credential.response.clientDataJSON
    # Authentication: credential.response.clientDataJSON
    response = credential.get("response", {})
    client_data_b64 = response.get("clientDataJSON", "")
    if not client_data_b64:
        raise ValueError("missing clientDataJSON")

    # Add padding if needed.
    padded = client_data_b64 + "=" * (4 - len(client_data_b64) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return json.loads(raw)


def _b64url_to_bytes(val: str) -> bytes:
    """Convert a base64url string to bytes."""
    import base64

    padded = val + "=" * (4 - len(val) % 4)
    return base64.urlsafe_b64decode(padded)
