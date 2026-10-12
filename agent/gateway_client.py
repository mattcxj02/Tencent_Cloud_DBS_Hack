"""The agent's only way to reach the gateway: READ and DRAFT calls over HTTP.

Every call carries the delegation token the gateway handed the agent for this run. The
token fixes the user and the conversation; nothing here can choose another user, append
a user turn, approve, sign or execute. The ``httpx.Client`` is injected so tests can use
``httpx.MockTransport`` or the real gateway's ``TestClient``.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import httpx

from shared.deadline import DEADLINE_HEADER, check_deadline, current_deadline, remaining_timeout

__all__ = ["DraftRejected", "GatewayClient", "GatewayError"]


class GatewayError(Exception):
    """A gateway call failed. ``code`` is a short stable string, never a secret."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


class DraftRejected(GatewayError):
    """``POST /internal/drafts`` returned 400 with a stable rejection code."""


class GatewayClient:
    def __init__(self, http: httpx.Client, token: str) -> None:
        if not token:
            raise ValueError("delegation token required")
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}

    # ── transport ────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        check_deadline()
        headers = dict(self._headers)
        budget = current_deadline.get()
        if budget is not None:
            headers[DEADLINE_HEADER] = budget.header()
            kwargs["timeout"] = remaining_timeout(15.0)
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise GatewayError(0, "GATEWAY_UNAVAILABLE") from None
        check_deadline()
        if resp.is_success:
            return resp.json()
        raise GatewayError(resp.status_code, _error_code(resp))

    # ── conversation ─────────────────────────────────────────────────

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/internal/conversations/{conversation_id}/snapshot")

    def report_progress(self, conversation_id: str, revision: int, stage: str) -> None:
        # Progress loss cannot alter payment eligibility.
        with suppress(GatewayError):
            self._request(
                "POST",
                f"/internal/conversations/{conversation_id}/progress",
                json={"revision": revision, "stage": stage},
            )

    def propose_question(
        self,
        conversation_id: str,
        text: str,
        *,
        revision: int | None = None,
        fields: list[dict] | None = None,
        extraction: dict | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/conversations/{conversation_id}/questions",
            json={
                "text": text,
                "revision": revision,
                "fields": fields or [],
                "extraction": extraction,
            },
        )

    # ── bank reads ───────────────────────────────────────────────────

    def list_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/internal/bank/accounts")

    def search_payees(self, query: str) -> list[dict[str, Any]]:
        return self._request("GET", "/internal/bank/payees/search", params={"q": query})

    def list_payees(self) -> list[dict[str, Any]]:
        return self._request("GET", "/internal/bank/payees")

    def list_billers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/internal/bank/billers")

    def list_cards(self) -> list[dict[str, Any]]:
        return self._request("GET", "/internal/bank/cards")

    def issue_quote(self, symbol: str) -> dict[str, Any]:
        """Creates an immutable mock quote; it moves no money."""
        return self._request("POST", "/internal/bank/quotes", json={"symbol": symbol})

    # ── draft proposal ───────────────────────────────────────────────

    def freeze_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        """Propose an untrusted draft. The gateway re-checks and freezes it, or rejects it."""
        try:
            return self._request("POST", "/internal/drafts", json=body)
        except GatewayError as exc:
            if exc.status == 400:
                raise DraftRejected(exc.status, exc.code) from None
            raise

    # ── agent-reported audit events ──────────────────────────────────

    def report_event(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> bool:
        """Best effort: an unreported agent event must not change the outcome."""
        body: dict[str, Any] = {"step": step, "payload": payload}
        if trace_id:
            body["trace_id"] = trace_id
        if span_id:
            body["span_id"] = span_id
        try:
            self._request("POST", "/internal/audit/agent-events", json=body)
        except GatewayError:
            return False
        return True


def _error_code(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return f"HTTP_{resp.status_code}"
    if isinstance(detail, str):
        return detail[:120]
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"][:120]
    if isinstance(detail, list):
        return "REQUEST_INVALID"
    return f"HTTP_{resp.status_code}"
