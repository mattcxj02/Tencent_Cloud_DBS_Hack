"""Payees page routes: saved payees with aliases, and customer nicknames.

- ``GET /api/payees`` → ``[{payee_id, display_name, bank, masked_suffix, kind, aliases,
  nicknames}]`` for the session user's own payees.
- ``POST /api/payees/{payee_id}/nicknames`` ``{"nickname": "mom"}`` → the updated payee.
- ``DELETE /api/payees/{payee_id}/nicknames/{nickname}`` → the updated payee.

Session required; mutating routes check Origin; every change is audited
(``gateway.payee_nickname_added`` / ``_removed``). Errors: ``{"detail": <code>}`` with
``PAYEE_NOT_FOUND`` / ``NICKNAME_NOT_FOUND`` (404), ``NICKNAME_EXISTS`` /
``NICKNAME_CONFLICT`` (409), ``NICKNAME_INVALID`` / ``NICKNAME_RESERVED`` /
``NICKNAME_LIMIT`` (422). Nicknames are names, not authority: every payment to the
payee still needs the confirmation screen and the customer's passkey.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank_routes import get_bank_store
from gateway.identity_routes import _check_origin
from gateway.payees import (
    MAX_NICKNAME_LEN,
    NicknameError,
    add_nickname,
    list_payees,
    remove_nickname,
)

router = APIRouter(tags=["payees"])

User = Annotated[UserPrincipal, Depends(require_user_session)]
_NO_STORE = {"Cache-Control": "no-store"}


class NicknameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str = Field(..., min_length=1, max_length=MAX_NICKNAME_LEN * 2)


def _db() -> str:
    return get_bank_store()._db_path


def _fail(exc: NicknameError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.code)


@router.get("/api/payees", response_model=None)
def get_payees(user: User) -> JSONResponse:
    return JSONResponse(list_payees(_db(), user.user_id), headers=_NO_STORE)


@router.post("/api/payees/{payee_id}/nicknames", response_model=None, status_code=201)
def post_nickname(
    payee_id: str, body: NicknameRequest, request: Request, user: User
) -> JSONResponse:
    _check_origin(request)
    try:
        payee: dict[str, Any] = add_nickname(_db(), user.user_id, payee_id, body.nickname)
    except NicknameError as exc:
        raise _fail(exc) from None
    return JSONResponse(payee, status_code=201, headers=_NO_STORE)


@router.delete("/api/payees/{payee_id}/nicknames/{nickname}", response_model=None)
def delete_nickname(payee_id: str, nickname: str, request: Request, user: User) -> JSONResponse:
    _check_origin(request)
    try:
        payee: dict[str, Any] = remove_nickname(_db(), user.user_id, payee_id, nickname)
    except NicknameError as exc:
        raise _fail(exc) from None
    return JSONResponse(payee, headers=_NO_STORE)
