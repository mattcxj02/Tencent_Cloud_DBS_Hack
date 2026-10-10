"""The agent's fixed LangGraph workflow.

    ingest → extract → normalize → resolve → propose → result
                          │           │          │
                          └───────────┴──────────┴──→ clarify → result

Routing is code. The only model call is in ``extract``; the model has no tools and only
copies words. Everything after it is deterministic:

- **ingest** reads the gateway snapshot and keeps current turns (replaced transcript
  originals dropped), bounded in size.
- **extract** asks the extractor model for quoted words (``prompts/extractor.md``).
- **normalize** checks the leg count and actions, locates every quote verbatim in a user
  turn (never inside a reference or quoted text), parses amounts with
  :mod:`shared.amounts` and links relative amounts ("half of that") to one earlier leg.
- **resolve** looks up recipients, billers, cards, stocks and source accounts over the
  bank READ API, returning *all* candidates; one candidate or ask. It narrows each
  citation to words naming exactly one record, fetches card outstanding amounts and
  requests equity quotes.
- **propose** submits the untrusted draft to ``POST /internal/drafts``. The gateway
  re-verifies everything and freezes it, or rejects it with a code that becomes a
  question.
- **clarify** records one short question through the gateway.

The agent never validates, approves, signs or executes; a frozen draft still needs the
gateway's independent validation, policy checks and the user's passkey approval.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import questions as Q
from agent import tracing
from agent.extraction import (
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_TURNS,
    ExtractedIntent,
    ExtractorLLM,
    ExtractorOutput,
    IntentAction,
    build_extraction_input,
    extract_intents,
)
from agent.gateway_client import DraftRejected, GatewayClient, GatewayError
from agent.names import (
    Record,
    account_record,
    biller_record,
    card_record,
    match_records,
    mentions,
    narrow_citation,
    payee_queries,
    payee_record,
    sgx_records,
)
from agent.prompt import ExtractorPrompt, load_extractor_prompt
from agent.questions import Issue
from agent.turns import LocateError, Span, VisibleTurn, locate, quoted_regions, visible_turns
from shared.amounts import (
    PRONOUN_REFERENTS,
    AmountRejected,
    FractionAmount,
    LiteralAmount,
    OutstandingAmount,
    ParsedAmount,
    RejectReason,
    RemainingAmount,
    parse_amount,
)
from shared.money import Money
from shared.schemas import MAX_LEGS, MAX_REFERENCE_LEN, Action, TargetType

if TYPE_CHECKING:
    from shared.telemetry import Telemetry, TraceParent

__all__ = ["AgentDeps", "AgentState", "LegPlan", "build_workflow", "run_agent"]

_TARGET_TYPES: dict[str, TargetType] = {
    "own_account": TargetType.own_account,
    "bank_payee": TargetType.saved_payee,
    "paynow_payee": TargetType.saved_paynow,
    "biller": TargetType.biller,
    "card": TargetType.owned_card,
    "sgx": TargetType.sgx_symbol,
}
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3}
_LATEST = {"last", "previous", "latest"}


# ── State ─────────────────────────────────────────────────────────────


@dataclass
class LegPlan:
    """Working record for one requested action. Built only by trusted code."""

    index: int
    intent: ExtractedIntent
    action: Action
    amount: ParsedAmount | None = None
    amount_span: Span | None = None
    recipient_span: Span | None = None
    source_span: Span | None = None
    reference_span: Span | None = None
    fraction_of: int | None = None
    target: Record | None = None
    target_cite: Span | None = None
    source: Record | None = None
    source_cite: Span | None = None
    amount_expr: dict[str, Any] | None = None
    quote_id: str | None = None

    @property
    def leg_id(self) -> str:
        return f"leg-{self.index + 1}"

    def name(self) -> str:
        """How questions refer to this payment, using the user's own words."""
        who = Q.quote_words(self.recipient_span.text) if self.recipient_span else None
        if self.action is Action.transfer:
            return f"the transfer to {who}" if who else "the transfer"
        if self.action is Action.pay_bill:
            return f"the bill payment to {who}" if who else "the bill payment"
        if self.action is Action.pay_card:
            return "the card payment"
        return f"the {who} share purchase" if who else "the share purchase"

    def amount_label(self) -> str:
        amount = Q.quote_words(self.amount_span.text) if self.amount_span else "the amount"
        if self.recipient_span:
            return f"{amount} for {Q.quote_words(self.recipient_span.text)}"
        return amount


class AgentState(TypedDict, total=False):
    conversation_id: str
    revision: int
    turns: list[VisibleTurn]
    extraction: ExtractorOutput
    plans: list[LegPlan]
    accounts: list[Record]
    issue: Issue | None
    error: str
    outcome: dict[str, Any]


@dataclass
class AgentDeps:
    """Everything the workflow talks to; injected so tests can fake both sides."""

    gateway: GatewayClient
    llm: ExtractorLLM
    prompt: ExtractorPrompt = field(default_factory=load_extractor_prompt)
    #: T-08 Langfuse telemetry; ``None`` disables tracing (tests). Never affects outcomes.
    telemetry: Telemetry | None = None


# ── Normalization (pure) ──────────────────────────────────────────────


def check_intents(output: ExtractorOutput) -> Issue | None:
    if not output.intents:
        return Issue("NO_INTENT", Q.NO_INTENT)
    if len(output.intents) > MAX_LEGS:
        return Issue("TOO_MANY_LEGS", Q.too_many_legs(len(output.intents), MAX_LEGS))
    for i, intent in enumerate(output.intents):
        if intent.action is IntentAction.unsupported:
            return Issue("UNSUPPORTED_ACTION", Q.UNSUPPORTED_ACTION, i)
    return None


def normalize_plans(
    intents: list[ExtractedIntent], turns: list[VisibleTurn]
) -> tuple[list[LegPlan], Issue | None]:
    """Locate quotes, parse amounts and link relative amounts. Stops at the first issue."""
    plans = [LegPlan(i, it, Action(it.action.value)) for i, it in enumerate(intents)]
    blocked = quoted_regions(turns)
    for plan in plans:
        quote = plan.intent.reference_quote
        if quote is None:
            continue
        span = locate(quote, turns)
        if isinstance(span, LocateError):
            return plans, Issue(span.value, Q.quote_not_found(plan.name()), plan.index)
        if len(span.text) > MAX_REFERENCE_LEN:
            return plans, Issue("REFERENCE_TOO_LONG", Q.reference_too_long(MAX_REFERENCE_LEN))
        plan.reference_span = span
        blocked.append(span)

    # Amount and recipient words are cited once per leg where possible ("50 to Mum and 50
    # to Ah Ma"); a shared source ("... from savings") is cited nearest to each recipient.
    used: dict[str, list[Span]] = {"recipient": [], "amount": [], "source": []}
    for plan in plans:
        issue = _normalize_leg(plan, plans, turns, blocked, used)
        if issue is not None:
            return plans, issue
    return plans, None


def _normalize_leg(
    plan: LegPlan,
    plans: list[LegPlan],
    turns: list[VisibleTurn],
    blocked: list[Span],
    used: dict[str, list[Span]],
) -> Issue | None:
    intent = plan.intent

    def find(quote: str, kind: str, anchor: Span | None = None) -> Span | Issue:
        prefer_unused = used[kind] if kind != "source" else ()
        span = locate(quote, turns, anchor=anchor, used=prefer_unused, blocked=blocked)
        if span is LocateError.IN_REFERENCE:
            return Issue(span.value, Q.quote_in_reference(plan.name()), plan.index)
        if isinstance(span, LocateError):
            return Issue(span.value, Q.quote_not_found(plan.name()), plan.index)
        used[kind].append(span)
        return span

    if intent.recipient_quote:
        found = find(intent.recipient_quote, "recipient")
        if isinstance(found, Issue):
            return found
        plan.recipient_span = found

    if not intent.amount_quote:
        return Issue("AMOUNT_MISSING", Q.amount_missing(plan.name()), plan.index)
    found = find(intent.amount_quote, "amount", anchor=plan.recipient_span)
    if isinstance(found, Issue):
        return found
    plan.amount_span = found
    issue = _interpret_amount(plan, plans, turns, blocked)
    if issue is not None:
        return issue

    if plan.recipient_span is None:
        # "pay my card balance": the card is named inside the amount words.
        if plan.action is Action.pay_card and mentions(found.text, ("card", "credit card")):
            plan.recipient_span = found
        else:
            return Issue("RECIPIENT_MISSING", Q.recipient_missing(plan.name()), plan.index)

    if intent.source_quote:
        src = find(intent.source_quote, "source", anchor=plan.recipient_span)
        if isinstance(src, Issue):
            return src
        plan.source_span = src
    return None  # a missing source is asked in resolve, where the account list is known


def _interpret_amount(
    plan: LegPlan, plans: list[LegPlan], turns: list[VisibleTurn], blocked: list[Span]
) -> Issue | None:
    assert plan.amount_span is not None
    words = plan.amount_span.text
    parsed = parse_amount(words)  # the user's own words, not the model's copy
    plan.amount = parsed
    if isinstance(parsed, AmountRejected):
        if parsed.reason is RejectReason.UNSUPPORTED_CURRENCY:
            question = Q.amount_unsupported_currency(plan.name())
            return Issue("AMOUNT_UNSUPPORTED_CURRENCY", question, plan.index)
        if parsed.reason is RejectReason.OUT_OF_RANGE:
            return Issue("AMOUNT_OUT_OF_RANGE", Q.amount_out_of_range(plan.name()), plan.index)
        return Issue("AMOUNT_UNCLEAR", Q.amount_unclear(plan.name(), words), plan.index)
    if isinstance(parsed, LiteralAmount):
        plan.amount_expr = {"kind": "literal", "amount": parsed.amount.to_str()}
        return None
    if isinstance(parsed, OutstandingAmount) and plan.action is not Action.pay_card:
        return Issue("AMOUNT_NEEDS_VALUE", Q.amount_needs_value(plan.name()), plan.index)
    if isinstance(parsed, FractionAmount):
        return _link_relative(plan, parsed, plans, turns, blocked)
    return None  # remaining balance / card outstanding: resolved against bank data


def _link_relative(
    plan: LegPlan,
    parsed: FractionAmount,
    plans: list[LegPlan],
    turns: list[VisibleTurn],
    blocked: list[Span],
) -> Issue | None:
    """Point "half of that" at exactly one earlier leg, or ask which one."""
    assert plan.amount_span is not None
    words = plan.amount_span.text
    earlier = plans[: plan.index]
    if not earlier:
        return Issue("RELATIVE_NO_EARLIER", Q.relative_no_earlier(words), plan.index)
    referent = parsed.referent
    if plan.intent.relative_to_quote:
        span = locate(
            plan.intent.relative_to_quote, turns, anchor=plan.amount_span, blocked=blocked
        )
        if isinstance(span, LocateError):
            return Issue(span.value, Q.quote_not_found(plan.name()), plan.index)
        referent = span.text
    if referent is None or referent.casefold().strip() in PRONOUN_REFERENTS:
        candidates = earlier
    else:
        candidates = _earlier_named_by(referent, earlier)
    if len(candidates) == 1:
        plan.fraction_of = candidates[0].index
        return None
    code = "RELATIVE_AMBIGUOUS" if candidates else "RELATIVE_NOT_FOUND"
    options = [c.amount_label() for c in (candidates or earlier)]
    return Issue(code, Q.relative_ambiguous(words, options), plan.index)


def _earlier_named_by(referent: str, earlier: list[LegPlan]) -> list[LegPlan]:
    words = set(re.findall(r"[a-z]+", referent.casefold()))
    ordinal_hits = {i for w, i in _ORDINALS.items() if w in words}
    if words & _LATEST:
        ordinal_hits.add(len(earlier) - 1)
    if ordinal_hits:
        return [earlier[i] for i in sorted(ordinal_hits) if i < len(earlier)]
    return [e for e in earlier if e.recipient_span and mentions(referent, [e.recipient_span.text])]


# ── Bank lookups ──────────────────────────────────────────────────────


class _Bank:
    """Per-run cache over the gateway's bank READ API."""

    def __init__(self, gateway: GatewayClient) -> None:
        self._gw = gateway
        self._accounts: list[Record] | None = None
        self._billers: list[Record] | None = None
        self._cards: list[Record] | None = None
        self._payees: dict[str, tuple[dict[str, Any], set[str]]] = {}
        self._searched: set[str] = set()

    def accounts(self) -> list[Record]:
        if self._accounts is None:
            self._accounts = [account_record(a) for a in self._gw.list_accounts()]
        return self._accounts

    def billers(self) -> list[Record]:
        if self._billers is None:
            self._billers = [biller_record(b) for b in self._gw.list_billers()]
        return self._billers

    def cards(self) -> list[Record]:
        if self._cards is None:
            self._cards = [card_record(c) for c in self._gw.list_cards()]
        return self._cards

    def payees_for(self, words: str) -> list[Record]:
        """All saved payees any search for these words returns (never one picked)."""
        for query in payee_queries(words):
            key = query.casefold()
            if key in self._searched:
                continue
            self._searched.add(key)
            for payee in self._gw.search_payees(query):
                _, aliases = self._payees.setdefault(payee["payee_id"], (payee, set()))
                if _norm(query) not in _norm(payee["display_name"]):
                    aliases.add(query)  # matched by alias, so the alias names this payee
        return self.known_payees()

    def known_payees(self) -> list[Record]:
        return [payee_record(p, sorted(aliases)) for p, aliases in self._payees.values()]

    def peers(self, kind: str) -> list[Record]:
        """All records the gateway compares a citation against for this kind."""
        if kind == "own_account":
            return self.accounts()
        if kind in ("bank_payee", "paynow_payee"):
            return [r for r in self.known_payees() if r.kind == kind]
        if kind == "biller":
            return self.billers()
        if kind == "card":
            return self.cards()
        return sgx_records()


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def resolve_leg(plan: LegPlan, plans: list[LegPlan], bank: _Bank) -> Issue | None:
    """Resolve target, source and amount expression for one leg, or return a question."""
    assert plan.recipient_span is not None
    issue = _resolve_target(plan, bank)
    if issue is not None:
        return issue
    issue = _resolve_source(plan, bank)
    if issue is not None:
        return issue
    return _resolve_amount(plan, plans, bank)


def _resolve_target(plan: LegPlan, bank: _Bank) -> Issue | None:
    assert plan.recipient_span is not None
    words = plan.recipient_span.text
    options: list[str] = []
    if plan.action is Action.transfer:
        own = match_records(words, bank.accounts())
        payees = match_records(words, bank.payees_for(words))
        candidates = own + payees if own and payees else own or payees
    elif plan.action is Action.pay_bill:
        candidates = match_records(words, bank.billers())
        options = [b.label for b in bank.billers()]
    elif plan.action is Action.pay_card:
        candidates = match_records(words, bank.cards())
        options = [c.label for c in bank.cards()]
    else:
        candidates = match_records(words, sgx_records())
        options = [s.label for s in sgx_records()]

    if not candidates:
        return Issue("RECIPIENT_NOT_FOUND", Q.recipient_not_found(words, options), plan.index)
    if len(candidates) > 1:
        labels = [c.label for c in candidates]
        return Issue("RECIPIENT_AMBIGUOUS", Q.recipient_ambiguous(words, labels), plan.index)
    target = candidates[0]
    cite = narrow_citation(plan.recipient_span, target, bank.peers(target.kind))
    if cite is None:
        return Issue("RECIPIENT_UNCITABLE", Q.recipient_uncitable(words), plan.index)
    plan.target, plan.target_cite = target, cite
    return None


def _resolve_source(plan: LegPlan, bank: _Bank) -> Issue | None:
    accounts = bank.accounts()
    labels = [a.label for a in accounts]
    if plan.source_span is None:
        return Issue("SOURCE_MISSING", Q.source_missing(plan.name(), labels), plan.index)
    words = plan.source_span.text
    matches = match_records(words, accounts)
    if len(matches) != 1:
        options = [m.label for m in matches] or labels
        return Issue("SOURCE_UNCLEAR", Q.source_unclear(words, options), plan.index)
    source = matches[0]
    cite = narrow_citation(plan.source_span, source, accounts)
    if cite is None:
        return Issue("SOURCE_UNCLEAR", Q.source_unclear(words, labels), plan.index)
    assert plan.target is not None
    if plan.target.kind == "own_account" and plan.target.record_id == source.record_id:
        return Issue("SAME_ACCOUNT", Q.same_account(plan.name()), plan.index)
    plan.source, plan.source_cite = source, cite
    return None


def _resolve_amount(plan: LegPlan, plans: list[LegPlan], bank: _Bank) -> Issue | None:
    amount = plan.amount
    assert plan.source is not None and plan.target is not None
    if isinstance(amount, FractionAmount):
        assert plan.fraction_of is not None
        plan.amount_expr = {
            "kind": "fraction_of_leg",
            "leg_id": plans[plan.fraction_of].leg_id,
            "numerator": amount.numerator,
            "denominator": amount.denominator,
        }
    elif isinstance(amount, RemainingAmount):
        referent = amount.referent
        if referent and referent.casefold() not in PRONOUN_REFERENTS:
            named = match_records(referent, bank.accounts())
            if [r.record_id for r in named] != [plan.source.record_id]:
                labels = [a.label for a in bank.accounts()]
                return Issue(
                    "REMAINING_ACCOUNT_UNCLEAR", Q.source_unclear(referent, labels), plan.index
                )
        plan.amount_expr = {"kind": "remaining_balance", "account_id": plan.source.record_id}
    elif isinstance(amount, OutstandingAmount):
        # The authoritative figure comes from the bank, not from the model or the user.
        outstanding = Money(plan.target.data["outstanding"])
        if outstanding.value == 0:
            question = Q.card_nothing_outstanding(plan.target.label)
            return Issue("CARD_NOTHING_OUTSTANDING", question, plan.index)
        plan.amount_expr = {"kind": "literal", "amount": outstanding.to_str()}
    return None


# ── Proposal ──────────────────────────────────────────────────────────


def leg_body(plan: LegPlan) -> dict[str, Any]:
    """The untrusted leg proposal in the gateway's ``LegInput`` shape."""
    assert plan.target and plan.source and plan.amount_span
    assert plan.target_cite and plan.source_cite and plan.amount_expr
    body: dict[str, Any] = {
        "leg_id": plan.leg_id,
        "action": plan.action.value,
        "source_account_id": plan.source.record_id,
        "target_type": _TARGET_TYPES[plan.target.kind].value,
        "target_id": plan.target.record_id,
        "amount_expr": plan.amount_expr,
        "evidence": {
            "amount": plan.amount_span.evidence(),
            "target": plan.target_cite.evidence(),
            "source": plan.source_cite.evidence(),
        },
    }
    if plan.reference_span is not None:
        body["reference"] = plan.reference_span.text
    if plan.action is Action.buy_equity:
        body["equity_terms"] = {"quote_id": plan.quote_id}
    return body


_RESTATE_CODES = {"EVIDENCE_SPAN_MISMATCH", "EVIDENCE_TURN_INVALID", "EVIDENCE_MISSING"}


def rejection_issue(code: str, plans: list[LegPlan], accounts: list[Record]) -> Issue | None:
    """Turn a gateway draft rejection into a question, or None if it is not the user's to fix."""
    payments = plans[0].name() if len(plans) == 1 else "each payment"
    if code in ("TARGET_EVIDENCE", "TARGET_EVIDENCE_AMBIGUOUS"):
        return Issue(f"GATEWAY_{code}", Q.gateway_recipient(payments))
    if code in ("SOURCE_EVIDENCE", "SOURCE_EVIDENCE_AMBIGUOUS"):
        return Issue(f"GATEWAY_{code}", Q.gateway_source([a.label for a in accounts]))
    if code == "AMOUNT_FROM_REFERENCE":
        return Issue(f"GATEWAY_{code}", Q.gateway_amount_reference(payments))
    if code == "RELATIVE_REFERENCE_INVALID":
        return Issue(f"GATEWAY_{code}", Q.gateway_relative())
    if code in _RESTATE_CODES:
        return Issue(f"GATEWAY_{code}", Q.EXTRACTION_RESTATE)
    return None


# ── Graph ─────────────────────────────────────────────────────────────


class _Nodes:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps

    def _event(self, step: str, payload: dict[str, Any]) -> None:
        trace_id, span_id = tracing.current_ids(self.d.telemetry)
        self.d.gateway.report_event(step, payload, trace_id=trace_id, span_id=span_id)

    # ingest ────────────────────────────────────────────────────────────

    def ingest(self, state: AgentState) -> dict[str, Any]:
        try:
            snapshot = self.d.gateway.snapshot(state["conversation_id"])
            turns = visible_turns(snapshot["turns"])
            revision = int(snapshot["revision"])
        except GatewayError as exc:
            return {
                "error": "GATEWAY_UNAVAILABLE" if exc.status in (0, 502, 503) else "SNAPSHOT_DENIED"
            }
        except (KeyError, TypeError, ValueError):
            return {"error": "SNAPSHOT_INVALID"}
        if not any(t.is_user for t in turns):
            return {"error": "NO_USER_INPUT"}
        if len(turns) > MAX_CONTEXT_TURNS or sum(len(t.text) for t in turns) > MAX_CONTEXT_CHARS:
            return {"error": "CONTEXT_LIMIT_EXCEEDED"}
        return {"revision": revision, "turns": turns}

    # extract ───────────────────────────────────────────────────────────

    def extract(self, state: AgentState) -> dict[str, Any]:
        turns = state["turns"]
        with tracing.generation(
            self.d.telemetry,
            "extractor",
            model=self.d.llm.model,
            prompt_version=self.d.prompt.version,
            prompt_hash=self.d.prompt.sha256,
            # The conversation data the model saw; the system prompt is identified by hash.
            input=json.loads(build_extraction_input(turns)),
            attributes={"conversation_revision": state["revision"]},
        ) as gen:
            result = extract_intents(self.d.llm, self.d.prompt, turns)
            gen.update(
                output=result.output.model_dump(mode="json") if result.output else None,
                usage=result.usage,
                latency_ms=result.latency_ms,
                status="error" if result.error else "ok",
                reason_code=result.error,
            )
        payload = {
            "conversation_revision": state["revision"],
            **result.audit_payload(self.d.prompt),
        }
        self._event("agent.extract", payload)
        if result.error is not None:
            return {"error": result.error}
        return {"extraction": result.output}

    # normalize ─────────────────────────────────────────────────────────

    def normalize(self, state: AgentState) -> dict[str, Any]:
        output = state["extraction"]
        issue = check_intents(output)
        plans: list[LegPlan] = []
        if issue is None:
            plans, issue = normalize_plans(output.intents, state["turns"])
        self._event(
            "agent.normalize",
            {
                "conversation_revision": state["revision"],
                "intents": len(output.intents),
                "actions": [i.action.value for i in output.intents][: MAX_LEGS + 6],
                "amount_kinds": [p.amount.kind if p.amount else None for p in plans],
                "outcome": issue.code if issue else "ok",
            },
        )
        return {"plans": plans, "issue": issue}

    # resolve ───────────────────────────────────────────────────────────

    def resolve(self, state: AgentState) -> dict[str, Any]:
        plans = state["plans"]
        bank = _Bank(self.d.gateway)
        issue: Issue | None = None
        try:
            for plan in plans:
                issue = resolve_leg(plan, plans, bank)
                if issue is not None:
                    break
            if issue is None:
                self._request_quotes(plans)
            accounts = bank.accounts()
        except (GatewayError, KeyError, TypeError, ValueError):
            self._event(
                "agent.resolve",
                {"conversation_revision": state["revision"], "outcome": "BANK_READ_FAILED"},
            )
            return {"error": "BANK_READ_FAILED"}
        self._event(
            "agent.resolve",
            {
                "conversation_revision": state["revision"],
                "legs": [
                    {
                        "leg_id": p.leg_id,
                        "target_type": _TARGET_TYPES[p.target.kind].value if p.target else None,
                        "target_id": p.target.record_id if p.target else None,
                        "source_account_id": p.source.record_id if p.source else None,
                        "amount_kind": (p.amount_expr or {}).get("kind"),
                        "quote_id": p.quote_id,
                    }
                    for p in plans
                ],
                "outcome": issue.code if issue else "ok",
            },
        )
        return {"plans": plans, "issue": issue, "accounts": accounts}

    # propose ───────────────────────────────────────────────────────────

    def propose(self, state: AgentState) -> dict[str, Any]:
        plans = state["plans"]
        try:
            frozen = self._freeze(state, plans)
        except DraftRejected as exc:
            self._event("agent.propose", self._propose_payload(state, exc.code))
            issue = rejection_issue(exc.code, plans, state.get("accounts", []))
            if issue is not None:
                return {"issue": issue}
            return {"outcome": {"status": "error", "code": exc.code}}
        except GatewayError as exc:
            self._event("agent.propose", self._propose_payload(state, exc.code))
            return {"outcome": {"status": "error", "code": "DRAFT_SUBMIT_FAILED"}}
        self._event(
            "agent.propose",
            {**self._propose_payload(state, "frozen"), "bundle_id": frozen.get("bundle_id")},
        )
        return {
            "outcome": {
                "status": "draft_frozen",
                "bundle_id": frozen.get("bundle_id"),
                "bundle_hash": frozen.get("bundle_hash"),
                "conversation_revision": frozen.get("conversation_revision"),
                "legs": frozen.get("legs", []),
            }
        }

    def _freeze(self, state: AgentState, plans: list[LegPlan]) -> dict[str, Any]:
        def submit() -> dict[str, Any]:
            return self.d.gateway.freeze_draft(
                {
                    "conversation_id": state["conversation_id"],
                    "conversation_revision": state["revision"],
                    "legs": [leg_body(p) for p in plans],
                }
            )

        try:
            return submit()
        except DraftRejected as exc:
            equity = [p for p in plans if p.action is Action.buy_equity]
            if exc.code != "QUOTE_EXPIRED" or not equity:
                raise
        self._request_quotes(equity)  # one fresh quote and one retry; then give up
        return submit()

    def _request_quotes(self, plans: list[LegPlan]) -> None:
        """Fresh mock quote per equity leg; the gateway freezes its terms."""
        for plan in plans:
            if plan.action is Action.buy_equity:
                assert plan.target is not None
                plan.quote_id = self.d.gateway.issue_quote(plan.target.record_id)["quote_id"]

    @staticmethod
    def _propose_payload(state: AgentState, outcome: str) -> dict[str, Any]:
        return {
            "conversation_revision": state["revision"],
            "legs": len(state["plans"]),
            "outcome": outcome,
        }

    # clarify / result ──────────────────────────────────────────────────

    def clarify(self, state: AgentState) -> dict[str, Any]:
        issue = state["issue"]
        assert issue is not None
        try:
            turn = self.d.gateway.propose_question(state["conversation_id"], issue.question)
        except GatewayError:
            return {"outcome": {"status": "error", "code": "QUESTION_FAILED"}}
        self._event(
            "agent.clarify",
            {
                "conversation_revision": state["revision"],
                "reason": issue.code,
                "leg": issue.leg,
                "question_turn_id": turn.get("turn_id"),
            },
        )
        return {
            "outcome": {
                "status": "clarify",
                "question_turn_id": turn.get("turn_id"),
                "question": issue.question,
                "reason": issue.code,
            }
        }

    def result(self, state: AgentState) -> dict[str, Any]:
        outcome = state.get("outcome") or {"status": "error", "code": state.get("error", "UNKNOWN")}
        return {"outcome": outcome}


def _route(next_node: str) -> Callable[[AgentState], str]:
    def route(state: AgentState) -> str:
        if state.get("error") or state.get("outcome"):
            return "result"
        if state.get("issue"):
            return "clarify"
        return next_node

    route.__name__ = f"route_to_{next_node}"
    return route


def _traced(telemetry: Telemetry | None, name: str, node: Callable[[AgentState], Any]) -> Any:
    """Run a node inside its own workflow-stage span."""

    def run(state: AgentState) -> dict[str, Any]:
        attributes = {"conversation_revision": state["revision"]} if "revision" in state else None
        with tracing.span(telemetry, name, attributes=attributes) as span:
            update = node(state)
            tracing.record_update(span, update)
        return update

    run.__name__ = name
    return run


def build_workflow(deps: AgentDeps) -> Any:
    """Compile the fixed graph. Nodes and edges never depend on model output."""
    nodes = _Nodes(deps)
    graph = StateGraph(AgentState)
    for name in ("ingest", "extract", "normalize", "resolve", "propose", "clarify"):
        graph.add_node(name, _traced(deps.telemetry, name, getattr(nodes, name)))
    graph.add_node("result", nodes.result)
    graph.add_edge(START, "ingest")
    graph.add_conditional_edges("ingest", _route("extract"), ["extract", "result"])
    graph.add_conditional_edges("extract", _route("normalize"), ["normalize", "result"])
    graph.add_conditional_edges("normalize", _route("resolve"), ["resolve", "clarify", "result"])
    graph.add_conditional_edges("resolve", _route("propose"), ["propose", "clarify", "result"])
    graph.add_conditional_edges("propose", _route("result"), ["clarify", "result"])
    graph.add_edge("clarify", "result")
    graph.add_edge("result", END)
    return graph.compile()


def run_agent(
    conversation_id: str, deps: AgentDeps, *, parent: TraceParent | None = None
) -> dict[str, Any]:
    """Run the workflow once for the conversation's current revision.

    ``parent`` continues the gateway's trace (validated ``traceparent``)."""
    graph = build_workflow(deps)
    with tracing.span(
        deps.telemetry,
        "agent.run",
        session_id=conversation_id,
        attributes={"conversation_id": conversation_id},
        parent=parent,
    ) as span:
        outcome = graph.invoke({"conversation_id": conversation_id}, {"recursion_limit": 16})[
            "outcome"
        ]
        tracing.record_outcome(span, outcome)
        if span.trace_id:
            outcome = {**outcome, "trace_id": span.trace_id}
    return outcome
