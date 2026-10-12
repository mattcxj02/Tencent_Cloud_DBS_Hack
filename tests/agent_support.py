"""Test doubles for the T-09 agent: a fake gateway over ``httpx.MockTransport`` and a
scripted extractor LLM.

The fake gateway mirrors the real gateway's contract closely enough to catch citation
mistakes: spans must sit exactly in a current user turn, target/source words must name
exactly one record of that kind (flat rule, as in ``gateway/evidence.py``), amounts may
not come from the leg's reference, and the revision must be current. The real gateway
is exercised separately in ``test_agent_gateway_integration.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx

from agent.extraction import LLMReply
from agent.gateway_client import GatewayClient
from agent.workflow import AgentDeps, run_agent
from shared.names import fuzzy_mentions, mentions, normalize

TOKEN = "dcta_fake-delegation-token"
TS = "2026-10-10T00:00:00.000Z"


def _account(account_id: str, kind: str, suffix: str, balance: str) -> dict[str, Any]:
    return {"account_id": account_id, "type": kind, "masked_suffix": suffix, "balance": balance}


def _payee(payee_id: str, name: str, bank: str | None, suffix: str, kind: str = "bank") -> dict:
    return {
        "payee_id": payee_id,
        "display_name": name,
        "bank": bank,
        "masked_suffix": suffix,
        "kind": kind,
    }


def _biller(biller_id: str, name: str, category: str, reference: str) -> dict[str, Any]:
    return {
        "biller_id": biller_id,
        "display_name": name,
        "category": category,
        "bill_reference": reference,
        "saved_at": TS,
    }


# Mirrors ledger/seed.py for user-matthew.
ACCOUNTS = [
    _account("acct-matthew-savings", "savings", "1234", "12000.00"),
    _account("acct-matthew-current", "current", "5678", "3500.00"),
    _account("acct-joint-01", "joint", "9012", "2000.00"),
]
PAYEES = [
    _payee("payee-mum", "Mum", "DBS", "9988"),
    _payee("payee-ahma", "Ah Ma", "POSB", "7766"),
    _payee("payee-johndoe", "John Doe", "DBS", "4521"),
    _payee("payee-johntan", "John Tan", "OCBC", "8892"),
    _payee("payee-lim", "Landlord Lim", "DBS", "3344"),
    {**_payee("payee-northwind", "Northwind Trading", "CITI", "1100"), "sanctioned": True},
    _payee("payee-priya", "Priya", None, "**12", kind="paynow"),
]
ALIASES = {
    "mum": "payee-mum",
    "ah ma": "payee-ahma",
    "mom": "payee-mum",
    "mummy": "payee-mum",
    "mommy": "payee-mum",
    "mama": "payee-mum",
    "ah mah": "payee-ahma",
    "amah": "payee-ahma",
    "grandma": "payee-ahma",
}
#: Customer nicknames (payee_nicknames); tests add to this per FakeGateway instance.
NICKNAMES: dict[str, str] = {}
BILLERS = [
    _biller("biller-spgroup", "SP Group", "utilities", "SP-REF-2026-001"),
    _biller("biller-singtel", "Singtel", "telecom", "SG-REF-2026-002"),
]
CARDS = [{"card_id": "card-matthew-01", "masked_suffix": "4242", "outstanding": "642.30"}]
PRICES = {"D05": "38.50", "O39": "9.55", "U11": "28.30", "Z74": "12.90"}
SGX_NAMES = {"D05": ("dbs",), "O39": ("ocbc",), "U11": ("uob",), "Z74": ("singtel",)}

USER_ROLES = {"user_text", "user_asr_final", "user_transcript_edit", "user_answer"}


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    return mentions(text, names)  # the gateway's rule (shared)


class Rejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class FakeGateway:
    """In-memory gateway for one user (Matthew) and one conversation."""

    def __init__(self, conversation_id: str = "conv-1") -> None:
        self.conversation_id = conversation_id
        self.turns: list[dict[str, Any]] = []
        self.revision = 0
        self.events: list[dict[str, Any]] = []
        self.proposals: list[dict[str, Any]] = []
        self.frozen: list[dict[str, Any]] = []
        self.quotes: dict[str, dict[str, Any]] = {}
        self.quote_requests: list[str] = []
        self.searches: list[str] = []
        self.reject_next: list[str] = []
        self.requests: list[httpx.Request] = []
        self.fail_snapshot: int | None = None
        self.nicknames: dict[str, str] = dict(NICKNAMES)

    # ── user side (what the browser would append via /api) ──────────

    def _append(self, role: str, text: str, **extra: Any) -> dict[str, Any]:
        turn = {
            "turn_id": f"turn-{len(self.turns) + 1}",
            "seq": len(self.turns) + 1,
            "role": role,
            "text": text,
            "edits_turn_id": extra.get("edits_turn_id"),
            "answers_question_id": extra.get("answers_question_id"),
            "created_at": TS,
        }
        self.turns.append(turn)
        if role != "app_question":
            self.revision += 1
        return turn

    def say(self, text: str) -> dict[str, Any]:
        return self._append("user_text", text)

    def edit(self, turn_id: str, text: str) -> dict[str, Any]:
        return self._append("user_transcript_edit", text, edits_turn_id=turn_id)

    def answer(self, text: str) -> dict[str, Any]:
        question = [t for t in self.turns if t["role"] == "app_question"][-1]
        return self._append("user_answer", text, answers_question_id=question["turn_id"])

    @property
    def questions(self) -> list[dict[str, Any]]:
        return [t for t in self.turns if t["role"] == "app_question"]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url="http://gateway")

    # ── HTTP handler ─────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "invalid token"})
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        conv = f"/internal/conversations/{self.conversation_id}"
        if method == "GET" and path == f"{conv}/snapshot":
            if self.fail_snapshot:
                return httpx.Response(self.fail_snapshot, json={"detail": "not found"})
            return httpx.Response(200, json=self.snapshot())
        if method == "POST" and path == f"{conv}/questions":
            return httpx.Response(200, json=self._append("app_question", body["text"]))
        if method == "POST" and path == "/internal/audit/agent-events":
            if not body["step"].startswith("agent."):
                return httpx.Response(403, json={"detail": "forbidden step"})
            self.events.append(body)
            return httpx.Response(200, json={"seq": len(self.events)})
        if method == "GET" and path == "/internal/bank/accounts":
            return httpx.Response(
                200, json=[{**a, "role": "owner", "created_at": TS} for a in ACCOUNTS]
            )
        if method == "GET" and path == "/internal/bank/payees/search":
            query = parse_qs(request.url.query.decode())["q"][0]
            self.searches.append(query)
            return httpx.Response(200, json=self.search(query))
        if method == "GET" and path == "/internal/bank/billers":
            return httpx.Response(200, json=BILLERS)
        if method == "GET" and path == "/internal/bank/cards":
            return httpx.Response(200, json=CARDS)
        if method == "POST" and path == "/internal/bank/quotes":
            return self._quote(body["symbol"])
        if method == "POST" and path == "/internal/drafts":
            self.proposals.append(body)
            try:
                return httpx.Response(200, json=self.freeze(body))
            except Rejected as exc:
                return httpx.Response(400, json={"detail": exc.code})
        return httpx.Response(404, json={"detail": "not found"})

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "revision": self.revision,
            "conversation_hash": f"hash-{self.revision}",
            "turns": self.turns,
        }

    def names_of(self, payee_id: str) -> list[str]:
        return [n for n, pid in {**ALIASES, **self.nicknames}.items() if pid == payee_id]

    def search(self, query: str) -> list[dict[str, Any]]:
        """Same contract as ``BankStore.search_payees``: exact first, then near spellings."""
        q = query.strip()
        exact, fuzzy = [], []
        for p in PAYEES:
            extra = self.names_of(p["payee_id"])
            full = (p["display_name"], *extra)
            if q.casefold() in p["display_name"].casefold() or mentions(q, full):
                exact.append({"sanctioned": False, **p, "names": extra, "match": "exact"})
            elif fuzzy_mentions(q, (*full, *p["display_name"].split())):
                fuzzy.append({"sanctioned": False, **p, "names": extra, "match": "fuzzy"})
        return exact + fuzzy

    def _quote(self, symbol: str) -> httpx.Response:
        symbol = symbol.upper()
        if symbol not in PRICES:
            return httpx.Response(400, json={"detail": f"unsupported symbol: {symbol!r}"})
        quote_id = f"quote-{symbol}-{len(self.quotes) + 1}"
        quote = {
            "quote_id": quote_id,
            "symbol": symbol,
            "unit_price": PRICES[symbol],
            "quoted_at": TS,
            "expires_at": "2026-10-10T00:05:00.000Z",
        }
        self.quotes[quote_id] = quote
        self.quote_requests.append(symbol)
        return httpx.Response(200, json=quote)

    # ── freeze: a simplified copy of the gateway's checks ────────────

    def _current_user_turns(self) -> dict[str, dict[str, Any]]:
        replaced = {t["edits_turn_id"] for t in self.turns if t["edits_turn_id"]}
        return {
            t["turn_id"]: t
            for t in self.turns
            if t["turn_id"] not in replaced and t["role"] in USER_ROLES
        }

    def _cite(self, span: dict[str, Any]) -> None:
        turn = self._current_user_turns().get(span["turn_id"])
        if turn is None:
            raise Rejected("EVIDENCE_TURN_INVALID")
        if not span["text"].strip() or turn["text"][span["start"] : span["end"]] != span["text"]:
            raise Rejected("EVIDENCE_SPAN_MISMATCH")

    def _candidates(self, target_type: str) -> dict[str, tuple[str, ...]]:
        if target_type == "own_account":
            return {a["account_id"]: (a["type"], a["masked_suffix"]) for a in ACCOUNTS}
        if target_type in ("saved_payee", "saved_paynow"):
            kind = "paynow" if target_type == "saved_paynow" else "bank"
            out = {}
            for p in PAYEES:
                if p["kind"] != kind:
                    continue
                aliases = tuple(self.names_of(p["payee_id"]))
                suffix = (p["masked_suffix"],) if p["masked_suffix"] else ()
                out[p["payee_id"]] = (
                    p["display_name"],
                    *p["display_name"].split(),
                    *suffix,
                    *aliases,
                )
            return out
        if target_type == "biller":
            return {
                b["biller_id"]: (b["display_name"], b["display_name"].split()[0]) for b in BILLERS
            }
        if target_type == "owned_card":
            return {c["card_id"]: ("card", "credit card", c["masked_suffix"]) for c in CARDS}
        if target_type == "sgx_symbol":
            return {s: (s, *n) for s, n in SGX_NAMES.items()}
        raise Rejected("UNKNOWN_TARGET_TYPE")

    def _identify(self, target_type: str, record_id: str, span: dict[str, Any], code: str) -> None:
        self._cite(span)
        matches = [
            r
            for r, names in self._candidates(target_type).items()
            if _mentions(span["text"], names)
        ]
        if matches != [record_id]:
            raise Rejected(code if record_id not in matches else f"{code}_AMBIGUOUS")

    def freeze(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.reject_next:
            raise Rejected(self.reject_next.pop(0))
        if body["conversation_id"] != self.conversation_id:
            raise Rejected("TOKEN_CONVERSATION_MISMATCH")
        if body["conversation_revision"] != self.revision:
            raise Rejected("STALE_CONVERSATION_REVISION")
        legs = body["legs"]
        if not 1 <= len(legs) <= 4:
            raise Rejected("LEG_COUNT")
        balances = {a["account_id"]: Decimal(a["balance"]) for a in ACCOUNTS}
        resolved: dict[str, Decimal] = {}
        debits: dict[str, Decimal] = {}
        out_legs = []
        for leg in legs:
            ev = leg.get("evidence") or {}
            if not all(k in ev for k in ("amount", "target", "source")):
                raise Rejected("EVIDENCE_MISSING")
            self._cite(ev["amount"])
            ref = (leg.get("reference") or "").strip()
            if ref and normalize(ev["amount"]["text"]) in normalize(ref):
                raise Rejected("AMOUNT_FROM_REFERENCE")
            self._identify(leg["target_type"], leg["target_id"], ev["target"], "TARGET_EVIDENCE")
            self._identify("own_account", leg["source_account_id"], ev["source"], "SOURCE_EVIDENCE")
            expr = leg["amount_expr"]
            if expr["kind"] == "literal":
                amount = Decimal(expr["amount"])
            elif expr["kind"] == "fraction_of_leg":
                if expr["leg_id"] not in resolved:
                    raise Rejected("RELATIVE_REFERENCE_INVALID")
                amount = (
                    resolved[expr["leg_id"]] * expr["numerator"] / expr["denominator"]
                ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            elif expr["kind"] == "remaining_balance":
                acct = expr["account_id"]
                amount = balances[acct] - debits.get(acct, Decimal(0))
            else:
                raise Rejected("AMOUNT_EXPR_INVALID")
            if leg["action"] == "buy_equity":
                quote = self.quotes.get((leg.get("equity_terms") or {}).get("quote_id"))
                if quote is None or quote["symbol"] != leg["target_id"]:
                    raise Rejected("QUOTE_SYMBOL_MISMATCH")
            resolved[leg["leg_id"]] = amount
            debits[leg["source_account_id"]] = (
                debits.get(leg["source_account_id"], Decimal(0)) + amount
            )
            out_legs.append(
                {
                    "leg_id": leg["leg_id"],
                    "action": leg["action"],
                    "source_account_id": leg["source_account_id"],
                    "target_type": leg["target_type"],
                    "target_id": leg["target_id"],
                    "amount": f"{amount:.2f}",
                    "currency": "SGD",
                    "reference": leg.get("reference"),
                }
            )
        bundle = {
            "bundle_id": f"bundle-{len(self.frozen) + 1}",
            "revision": 1,
            "conversation_id": self.conversation_id,
            "conversation_revision": self.revision,
            "conversation_hash": f"hash-{self.revision}",
            "legs": out_legs,
        }
        self.frozen.append(bundle)
        return bundle


# ── Scripted LLM ──────────────────────────────────────────────────────


Reply = str | dict | None | Exception | Callable[[str], Any]


class FakeLLM:
    """Returns scripted replies in order (the last one repeats). Records every call."""

    model = "fake-extractor"

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies) or [None]
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_content: str) -> LLMReply:
        self.calls.append((system_prompt, user_content))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(user_content)
        if isinstance(reply, dict):
            reply = json.dumps(reply)
        return LLMReply(content=reply, finish_reason="stop", usage={"total_tokens": 10})

    def conversation(self, call: int = -1) -> list[dict[str, Any]]:
        """The conversation lines the model saw in a given call."""
        return json.loads(self.calls[call][1])["conversation"]


def intent(
    action: str,
    amount: str | None = None,
    recipient: str | None = None,
    source: str | None = None,
    *,
    reference: str | None = None,
    relative_to: str | None = None,
    unclear: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "action": action,
        "amount_quote": amount,
        "recipient_quote": recipient,
        "source_quote": source,
        "reference_quote": reference,
        "relative_to_quote": relative_to,
        "unclear": list(unclear),
    }


def reply(*intents: dict[str, Any]) -> dict[str, Any]:
    return {"intents": list(intents), "unclear": []}


def run(gateway: FakeGateway, llm: FakeLLM) -> dict[str, Any]:
    with gateway.client() as http:
        return run_agent(gateway.conversation_id, AgentDeps(GatewayClient(http, TOKEN), llm))
