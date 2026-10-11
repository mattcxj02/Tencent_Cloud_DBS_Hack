"""Read-only browser routes for the web UI (session required, owner only).

- ``GET /api/accounts`` → ``[{account_id, display, balance}]`` for the balances panel:
  the user's own (and joint) accounts, ``display`` = type + masked suffix
  (``Savings ••1234``), balance as a 2-dp string.
- ``GET /api/conversations/{conversation_id}/evidence`` → the evidence view built by
  ``gateway/evidence_view.py`` (404 ``{"detail": "not found"}`` for unknown or other
  users' conversations). No secrets, assertions, codes or audio; text is unescaped and
  must be escaped by the UI.

Handlers are plain ``def`` (synchronous SQLite in the threadpool); responses are
``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank_routes import get_bank_store
from gateway.evidence_view import build_evidence
from gateway.orchestration import account_display

router = APIRouter(tags=["read"])

User = Annotated[UserPrincipal, Depends(require_user_session)]
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/accounts", response_model=None)
def list_accounts(user: User) -> JSONResponse:
    """The session user's accounts with masked display labels and balances."""
    accounts: list[dict[str, Any]] = get_bank_store().get_accounts(user.user_id)
    body = [
        {
            "account_id": a["account_id"],
            "display": account_display(a["type"], a["masked_suffix"]),
            "balance": a["balance"],
        }
        for a in accounts
    ]
    return JSONResponse(body, headers=_NO_STORE)


@router.get("/api/conversations/{conversation_id}/evidence", response_model=None)
def conversation_evidence(conversation_id: str, user: User) -> JSONResponse:
    """You said → We clarified → You approved → Executed, with audit and trace links."""
    view = build_evidence(get_bank_store()._db_path, user.user_id, conversation_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return JSONResponse(view, headers=_NO_STORE)
