"""Draft and authorization routes.

- ``POST /internal/drafts`` — freeze a draft from an agent proposal
  (delegation token, scope ``draft:freeze``).
- ``POST /api/drafts/{bundle_id}/authorizations`` — create a signing
  envelope (user session required).
- ``GET /api/authorizations/{authz_id}`` — get authorization status +
  envelope (user session, user-scoped).
- ``POST /api/authorizations/{authz_id}/approve`` — approve with a
  WebAuthn assertion (user session + origin check); on success the
  atomic commit runs and the persisted receipt is returned.
- ``GET /api/authorizations/{authz_id}/receipt`` — the stored receipt of a
  committed authorization (owner only), for lost-response recovery.

The approve endpoint takes ``{authorization_id, expected_envelope_hash,
assertion}`` and **never** accepts replacement payload fields.

Handlers are plain ``def`` so FastAPI runs them in its threadpool: they use
synchronous SQLite, and money-moving requests serialize on the database
write lock (``BEGIN IMMEDIATE``), not on the event loop.

Errors carry a stable machine code in ``detail`` and, where relevant,
``reason_codes`` (see ``gateway.policy.REASON_CODES``).  Clients must key on
these codes, never on prose.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth.delegation import Principal, require_principal
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.drafts import (
    ApprovalError,
    DraftError,
    DraftStore,
    get_draft_store,
)
from gateway.identity_routes import _check_origin
from gateway.telemetry import stage
from shared.canonical import bundle_hash
from shared.telemetry import extract_trace_context

router = APIRouter(tags=["drafts"])


# ── Request/response models ───────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegInput(_Strict):
    leg_id: str = Field(..., min_length=1)
    action: str
    source_account_id: str = Field(..., min_length=1)
    target_type: str
    target_id: str = Field(..., min_length=1)
    amount_expr: dict[str, Any] | None = None
    amount: str | None = None
    reference: str | None = Field(default=None, max_length=140)
    equity_terms: dict[str, Any] | None = None
    # Untrusted citations: {"amount"|"target"|"source": {turn_id, start, end, text}}.
    evidence: dict[str, dict[str, Any]] | None = None


class FreezeDraftRequest(_Strict):
    conversation_id: str = Field(..., min_length=1)
    conversation_revision: int = Field(..., ge=1)
    legs: list[LegInput] = Field(..., min_length=1, max_length=4)


class CreateAuthorizationRequest(_Strict):
    # The operation is fixed to execute_bundle on this route; it is never client-chosen.
    credential_id: str = Field(..., min_length=1)


class ApproveRequest(_Strict):
    authorization_id: str = Field(..., min_length=1)
    expected_envelope_hash: str = Field(..., min_length=1)
    assertion: dict[str, Any]


# ── Module-level store (same pattern as bank/conv routes) ────────────


def _set_draft_store(store: DraftStore | None) -> None:
    """Test override: inject a store with a temp DB path."""
    from gateway.drafts import _set_draft_store as _set

    _set(store)


# ── Routes ────────────────────────────────────────────────────────────

DraftFreezePrincipal = Annotated[Principal, Depends(require_principal("draft:freeze"))]


@router.post("/internal/drafts")
def freeze_draft(
    body: FreezeDraftRequest,
    principal: DraftFreezePrincipal,
    request: Request,
) -> dict[str, Any]:
    """Freeze an agent proposal into an immutable draft bundle.

    Traced as the gateway's ``compile`` stage, continuing the agent's validated
    ``traceparent`` (correlation only; the token alone decides who may freeze)."""
    if principal.conversation_id != body.conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="TOKEN_CONVERSATION_MISMATCH"
        )
    store = get_draft_store()
    with stage(
        "compile",
        user_id=principal.user_id,
        conversation_id=body.conversation_id,
        attributes={
            "conversation_id": body.conversation_id,
            "conversation_revision": body.conversation_revision,
            "leg_count": len(body.legs),
        },
        parent=extract_trace_context(request.headers),
    ) as span:
        try:
            bundle = store.freeze_draft(
                user_id=principal.user_id,
                conversation_id=body.conversation_id,
                conversation_revision=body.conversation_revision,
                legs=[leg.model_dump() for leg in body.legs],
            )
        except DraftError as exc:
            code = str(exc)
            span.set_status(
                "superseded" if code == "STALE_CONVERSATION_REVISION" else "rejected",
                reason_code=code,  # free-text messages fail the reason-code check and drop
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=code,
            ) from None
        span.set_attributes(
            {"bundle_id": str(bundle.bundle_id), "bundle_hash": bundle_hash(bundle)}
        )

    return {
        "bundle_id": str(bundle.bundle_id),
        "revision": bundle.revision,
        "owner_user_id": bundle.owner_user_id,
        "conversation_id": bundle.conversation_id,
        "conversation_revision": bundle.conversation_revision,
        "conversation_hash": bundle.conversation_hash,
        "bundle_hash": bundle_hash(bundle),
        "created_at": bundle.created_at,
        "legs": [
            {
                "leg_id": leg.leg_id,
                "action": leg.action.value,
                "source_account_id": leg.source_account_id,
                "target_type": leg.target_type.value,
                "target_id": leg.target_id,
                "amount": leg.amount.to_str(),
                "currency": leg.currency,
                "display_name": leg.display_metadata.display_name,
                "masked_account_suffix": leg.display_metadata.masked_account_suffix,
                "reference": leg.reference,
            }
            for leg in bundle.legs
        ],
    }


def _authorization_view(store: DraftStore, authz: dict[str, Any]) -> dict[str, Any]:
    """What the client renders and signs: the stored envelope plus WebAuthn options."""
    return {
        "authorization_id": authz["authorization_id"],
        "status": authz["status"],
        "operation": authz["operation"],
        "envelope_hash": authz["envelope_hash"],
        "expires_at": authz["expires_at"],
        "reason_codes": authz.get("reason_codes", []),
        # Parsed from the exact stored bytes; the UI renders this, not a model summary.
        "envelope": json.loads(authz["envelope_bytes"]),
        "webauthn_options": store.webauthn_request_options(authz),
    }


#: Stable approval error codes that are not HTTP 409 Conflict.
_ERROR_HTTP_STATUS: dict[str, int] = {
    "AUTHORIZATION_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "DRAFT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "COMMIT_NOT_IMPLEMENTED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "COMMIT_FAILED": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _approval_error(exc: ApprovalError) -> JSONResponse:
    """``{"detail": <stable code>, "reason_codes": [...]}`` with a stable HTTP status."""
    code = str(exc)
    http = _ERROR_HTTP_STATUS.get(code, status.HTTP_409_CONFLICT)
    return JSONResponse(
        status_code=http,
        content={"detail": code, "reason_codes": list(getattr(exc, "reason_codes", ()))},
    )


@router.post("/api/drafts/{bundle_id}/authorizations", response_model=None)
def create_authorization(
    bundle_id: str,
    body: CreateAuthorizationRequest,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Issue the signing envelope for a frozen draft (refused on a policy BLOCK)."""
    _check_origin(request)
    store = get_draft_store()
    try:
        _, authz_id = store.create_authorization(
            user_id=user.user_id, bundle_id=bundle_id, credential_id=body.credential_id
        )
    except ApprovalError as exc:
        return _approval_error(exc)
    authz = store.get_authorization(authz_id, user.user_id)
    assert authz is not None
    return _authorization_view(store, authz)


@router.get("/api/authorizations/{authz_id}")
def get_authorization(
    authz_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> dict[str, Any]:
    """Get an authorization (owner only) with its exact stored envelope."""
    store = get_draft_store()
    authz = store.get_authorization(authz_id, user.user_id)
    if authz is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="authorization not found")
    return _authorization_view(store, authz)


@router.post("/api/authorizations/{authz_id}/approve", response_model=None)
def approve_authorization(
    authz_id: str,
    body: ApproveRequest,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """Approve with a WebAuthn assertion over the stored envelope.

    The body carries only the authorization id, the expected envelope hash and the
    assertion; it can never replace payload fields. A verified signature alone
    never commits: the policy pre-check runs, then the atomic recheck-and-commit.

    200 responses carry ``status``: ``COMMITTED`` (with ``receipt_id`` and the
    stored ``receipt``; ``replayed`` is true for an idempotent retry),
    ``AWAITING_STEP_UP`` or ``BLOCKED`` (with ``reason_codes``).
    """
    _check_origin(request)
    if body.authorization_id != authz_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="AUTHORIZATION_ID_MISMATCH"
        )
    store = get_draft_store()
    try:
        return store.approve_authorization(
            user_id=user.user_id,
            authz_id=authz_id,
            expected_envelope_hash=body.expected_envelope_hash,
            assertion=body.assertion,
        )
    except ApprovalError as exc:  # includes CommitUnavailableError → 503
        return _approval_error(exc)


@router.get("/api/authorizations/{authz_id}/receipt", response_model=None)
def get_receipt(
    authz_id: str,
    user: Annotated[UserPrincipal, Depends(require_user_session)],
) -> dict[str, Any] | JSONResponse:
    """The persisted receipt of a committed authorization (owner only).

    Safe to call after a lost approve response: it reads, never executes.
    404 ``AUTHORIZATION_NOT_FOUND`` for unknown or other users' IDs; 409
    ``NOT_COMMITTED`` (with the current ``status``) until a commit exists.
    """
    store = get_draft_store()
    authz = store.get_authorization(authz_id, user.user_id)
    if authz is None:
        return _approval_error(ApprovalError("AUTHORIZATION_NOT_FOUND"))
    receipt = store.get_receipt(authz_id, user.user_id)
    if receipt is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "NOT_COMMITTED",
                "status": authz["status"],
                "reason_codes": authz["reason_codes"],
            },
        )
    return {
        "authorization_id": authz_id,
        "status": receipt.status,
        "receipt_id": receipt.receipt_id,
        "receipt": dict(receipt.details),
    }
