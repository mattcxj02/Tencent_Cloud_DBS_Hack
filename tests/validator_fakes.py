"""Test-only validator fakes and request builders (T-10).

Nothing here is importable by the running services: images do not contain ``tests/``
and ``validator/`` never imports it (checked in test_t10_validator.py).

- :class:`ScriptedLLM` returns a fixed reply and records the messages it received.
- :class:`AlwaysMatchLLM` returns MATCH for every input. It exists to prove that a
  validator forced to MATCH still cannot bypass gateway approval and policy checks.
- :class:`RaisingLLM` / :class:`SlowLLM` simulate provider failure and timeouts.
- ``build_request`` and friends produce synthetic, schema-valid requests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any

from shared.canonical import canonical_bytes
from shared.clients.validator import ValidationRequest
from shared.settings import ValidatorSettings
from validator.llm import LLMReply

TOKEN = "test-validator-token-0123456789abcdefghij"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(token: str | None = TOKEN, timeout: float = 5.0) -> ValidatorSettings:
    """Settings that never read .env; the fake LLM is injected separately."""
    return ValidatorSettings(
        _env_file=None,
        VALIDATOR_SERVICE_TOKEN=token,
        VALIDATE_TIMEOUT_SECONDS=timeout,
        VALIDATE_PROVIDER="deepseek",
        VALIDATE_MODEL="fake-model",
        deepseek_key=None,
    )


class ScriptedLLM:
    def __init__(self, content: str | None, finish_reason: str | None = "stop", **extra: Any):
        self.content = content
        self.finish_reason = finish_reason
        self.extra = extra
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> LLMReply:
        self.calls.append(messages)
        return LLMReply(content=self.content, finish_reason=self.finish_reason, **self.extra)

    @classmethod
    def verdict(cls, verdict: str, issues: list[dict[str, Any]] | None = None) -> ScriptedLLM:
        return cls(json.dumps({"verdict": verdict, "issues": issues or []}))


class AlwaysMatchLLM(ScriptedLLM):
    """Test-only stub: MATCH for every input, whatever the conversation says."""

    def __init__(self) -> None:
        super().__init__(json.dumps({"verdict": "MATCH", "issues": []}))


class RaisingLLM:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> LLMReply:
        raise self.exc


class SlowLLM(ScriptedLLM):
    def __init__(self, delay: float) -> None:
        super().__init__(json.dumps({"verdict": "MATCH", "issues": []}))
        self.delay = delay

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> LLMReply:
        time.sleep(self.delay)
        return super().complete(messages, timeout_seconds=timeout_seconds)


# ── Request builders ────────────────────────────────────────────────

ACCOUNTS = [
    {"account_id": "acct-matthew-savings", "display": "Savings ••1234"},
    {"account_id": "acct-matthew-current", "display": "Current ••5678"},
]
TARGETS = [
    {"target_id": "payee-mum", "display": "Mum (DBS ••9988, saved payee)"},
    {"target_id": "payee-johndoe", "display": "John Doe (DBS ••4521, saved payee)"},
    {"target_id": "payee-johntan", "display": "John Tan (OCBC ••8892, saved payee)"},
    {"target_id": "biller-spgroup", "display": "SP Group (saved biller)"},
    {"target_id": "card-matthew-01", "display": "Card ••4242"},
    {"target_id": "D05", "display": "DBS Group Holdings (SGX: D05)"},
    {"target_id": "U11", "display": "UOB (SGX: U11)"},
]
_DISPLAY = {
    "payee-mum": ("Mum", "9988"),
    "payee-johndoe": ("John Doe", "4521"),
    "payee-johntan": ("John Tan", "8892"),
    "biller-spgroup": ("SP Group", None),
    "card-matthew-01": ("Card 4242", "4242"),
    "D05": ("SGX: D05", None),
    "U11": ("SGX: U11", None),
}
_TARGET_TYPE = {
    "transfer": "saved_payee",
    "pay_bill": "biller",
    "pay_card": "owned_card",
    "buy_equity": "sgx_symbol",
}


def turn(turn_id: str, seq: int, text: str, role: str = "user_text", **links: str) -> dict:
    return {"turn_id": turn_id, "seq": seq, "role": role, "text": text, **links}


def leg(
    leg_id: str,
    action: str,
    target_id: str,
    amount: str,
    source: str = "acct-matthew-savings",
    reference: str | None = None,
    equity: dict | None = None,
    display_name: str | None = None,
) -> dict:
    name, suffix = _DISPLAY.get(target_id, (target_id, None))
    out: dict[str, Any] = {
        "leg_id": leg_id,
        "action": action,
        "source_account_id": source,
        "target_type": _TARGET_TYPE[action],
        "target_id": target_id,
        "display_metadata": {"display_name": display_name or name, "masked_account_suffix": suffix},
        "amount": amount,
        "currency": "SGD",
    }
    if reference is not None:
        out["reference"] = reference
    if equity is not None:
        out["equity_terms"] = equity
    return out


def equity_terms(budget: str, unit_price: str, quantity: int, debit: str, residual: str) -> dict:
    return {
        "budget": budget,
        "quote_id": "quote-test",
        "unit_price": unit_price,
        "quoted_at": "2026-10-10T00:00:00.000Z",
        "quote_expires_at": "2026-10-10T00:02:00.000Z",
        "quantity": quantity,
        "actual_debit": debit,
        "residual_cash": residual,
    }


def request_dict(
    turns: list[dict],
    legs: list[dict],
    *,
    accounts: list[dict] | None = None,
    targets: list[dict] | None = None,
    conversation_id: str = "conv-test",
    revision: int | None = None,
) -> dict:
    revision = revision if revision is not None else len(turns)
    conv_hash = hashlib.sha256(canonical_bytes({"r": revision, "turns": turns})).hexdigest()
    return {
        "request_id": f"vreq-{uuid.uuid4().hex[:12]}",
        "conversation": {
            "conversation_id": conversation_id,
            "revision": revision,
            "conversation_hash": conv_hash,
            "turns": turns,
        },
        "bundle": {
            "bundle_id": str(uuid.UUID(int=1)),
            "revision": 1,
            "owner_user_id": "user-matthew",
            "conversation_id": conversation_id,
            "conversation_revision": revision,
            "conversation_hash": conv_hash,
            "created_at": "2026-10-10T00:00:00.000Z",
            "legs": legs,
        },
        "bank_metadata": {
            "accounts": copy.deepcopy(accounts if accounts is not None else ACCOUNTS),
            "targets": copy.deepcopy(targets if targets is not None else TARGETS),
        },
    }


def build_request(turns: list[dict], legs: list[dict], **kw: Any) -> ValidationRequest:
    return ValidationRequest.model_validate_json(json.dumps(request_dict(turns, legs, **kw)))


def simple_request(text: str = "Send 50 dollars to Mum from my savings") -> ValidationRequest:
    return build_request(
        [turn("turn-1", 1, text)], [leg("leg-1", "transfer", "payee-mum", "50.00")]
    )
