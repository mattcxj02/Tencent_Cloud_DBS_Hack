"""Gateway -> validator contract and HTTP client (T-10).

Only the gateway calls the internal semantic validator. This module holds what both
sides share:

- the request schema (full ordered conversation, the frozen bundle exactly as stored,
  and gateway-resolved bank labels). There is no field for extractor reasoning,
  confidence, summaries or prompt text, and unknown fields are rejected;
- the result schema and the stable error codes;
- the input hash (SHA-256 of the canonical request) that binds a result to exactly
  what was checked;
- the reference checks applied to model output (verdict/issue consistency, leg IDs,
  evidence spans). The validator applies them, and this client applies them again;
- :class:`ValidatorClient`, a small httpx client for the gateway.

A MATCH only makes a draft eligible for the confirmation screen. It is never an
authorization: user approval and deterministic gateway policy still apply (I8).
Every transport, HTTP or parsing failure becomes an ``ERROR`` result, never a MATCH.

The service token travels in ``Authorization: Bearer``. It is not logged and never
appears in a result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from shared.canonical import canonical_bytes
from shared.schemas import FrozenBundle

__all__ = [
    "VALIDATE_PATH",
    "MAX_TURNS",
    "MAX_TURN_CHARS",
    "MAX_TOTAL_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_ISSUES",
    "MAX_EVIDENCE_PER_ISSUE",
    "USER_ROLES",
    "ErrorCode",
    "Verdict",
    "IssueField",
    "ConversationTurn",
    "ConversationSnapshot",
    "AccountLabel",
    "TargetLabel",
    "BankMetadata",
    "ValidationRequest",
    "EvidenceSpan",
    "ValidationIssue",
    "VerdictPayload",
    "TokenUsage",
    "ValidationResult",
    "validation_input_hash",
    "request_problem",
    "verdict_problem",
    "ValidatorClient",
]

VALIDATE_PATH = "/internal/validate"

# ── Limits ───────────────────────────────────────────────────────────
# Context budget, matching the gateway conversation store. Exceeding it yields
# CONTEXT_LIMIT_RESTATE: the user must restate the request. Nothing is truncated.
MAX_TURNS = 40
MAX_TURN_CHARS = 2000
MAX_TOTAL_CHARS = 20_000
#: Hard cap on the raw request body (UTF-8 JSON); larger bodies are not read.
MAX_REQUEST_BYTES = 256 * 1024

MAX_ACCOUNTS = 20
MAX_TARGETS = 40
MAX_DISPLAY_CHARS = 200
MAX_ISSUES = 10
MAX_EVIDENCE_PER_ISSUE = 5
MAX_REASON_CHARS = 200
MAX_SUPPORTED_VALUE_CHARS = 100

# ── Enumerations ─────────────────────────────────────────────────────

TurnRole = Literal[
    "user_text",
    "user_asr_final",
    "user_transcript_edit",
    "app_question",
    "user_answer",
]
USER_ROLES = frozenset({"user_text", "user_asr_final", "user_transcript_edit", "user_answer"})

Verdict = Literal["MATCH", "MISMATCH", "UNCERTAIN"]

IssueField = Literal[
    "action",
    "recipient",
    "source",
    "amount",
    "currency",
    "asset",
    "quantity",
    "order",
    "dependency",
    "missing_leg",
    "extra_leg",
    "other",
]

ErrorCode = Literal[
    # Request rejected by the validator (HTTP 4xx/503); the model is not called.
    "UNAUTHORIZED",  # 401: missing or wrong service token
    "SERVICE_TOKEN_UNSET",  # 503: validator has no token configured; refuses everything
    "CONTEXT_LIMIT_RESTATE",  # 413: over the context budget; ask the user to restate
    "INVALID_REQUEST",  # 422: malformed JSON, unknown/missing fields, bad types
    "CONVERSATION_INVALID",  # 422: duplicate turn IDs, unordered seq, dangling edit/answer links
    "BINDING_MISMATCH",  # 422: bundle is bound to a different conversation/revision/hash
    "BANK_METADATA_INCOMPLETE",  # 422: a leg's source account or target has no bank label
    # Model call failed or its output was rejected (HTTP 200, status ERROR).
    "LLM_NOT_CONFIGURED",
    "LLM_TIMEOUT",
    "LLM_PROVIDER_ERROR",
    "LLM_EMPTY_OUTPUT",
    "LLM_OUTPUT_TRUNCATED",
    "LLM_ABNORMAL_FINISH",
    "LLM_UNEXPECTED_TOOL_CALL",
    "OUTPUT_NOT_JSON",
    "OUTPUT_SCHEMA_INVALID",
    "OUTPUT_VERDICT_INCONSISTENT",
    "OUTPUT_UNKNOWN_LEG",
    "OUTPUT_EVIDENCE_INVALID",
    # Produced by ValidatorClient on the gateway side.
    "VALIDATOR_TIMEOUT",
    "VALIDATOR_UNAVAILABLE",
    "VALIDATOR_HTTP_ERROR",
    "VALIDATOR_RESPONSE_INVALID",
    "INPUT_HASH_MISMATCH",
]

#: Codes the validator may put in a 4xx/503 body.
_REQUEST_ERROR_CODES = frozenset(
    {
        "UNAUTHORIZED",
        "SERVICE_TOKEN_UNSET",
        "CONTEXT_LIMIT_RESTATE",
        "INVALID_REQUEST",
        "CONVERSATION_INVALID",
        "BINDING_MISMATCH",
        "BANK_METADATA_INCOMPLETE",
    }
)

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# ── Request ──────────────────────────────────────────────────────────


class ConversationTurn(_Contract):
    """One gateway-captured turn. The text length budget is checked by request_problem."""

    turn_id: Id
    seq: int = Field(ge=1)
    role: TurnRole
    text: str = Field(min_length=1)
    edits_turn_id: Id | None = None
    answers_question_id: Id | None = None

    @model_validator(mode="after")
    def _links_match_role(self) -> ConversationTurn:
        if (self.role == "user_transcript_edit") != (self.edits_turn_id is not None):
            raise ValueError("edits_turn_id is required for, and only allowed on, transcript edits")
        if (self.role == "user_answer") != (self.answers_question_id is not None):
            raise ValueError("answers_question_id is required for, and only allowed on, answers")
        return self


class ConversationSnapshot(_Contract):
    """The full ordered conversation at one revision, as the gateway stores it."""

    conversation_id: Id
    revision: int = Field(ge=1)
    conversation_hash: Hex64
    turns: list[ConversationTurn] = Field(min_length=1)


class AccountLabel(_Contract):
    account_id: Id
    display: str = Field(min_length=1, max_length=MAX_DISPLAY_CHARS)


class TargetLabel(_Contract):
    target_id: Id
    display: str = Field(min_length=1, max_length=MAX_DISPLAY_CHARS)


class BankMetadata(_Contract):
    """Gateway-resolved labels. Untrusted text as far as the model is concerned.

    ``accounts``: the user's operable source accounts. ``targets``: every target in the
    bundle plus, optionally, other candidates for names the user used (for example both
    saved payees called John) so the validator can see an ambiguity.
    """

    accounts: list[AccountLabel] = Field(min_length=1, max_length=MAX_ACCOUNTS)
    targets: list[TargetLabel] = Field(min_length=1, max_length=MAX_TARGETS)

    @model_validator(mode="after")
    def _unique_ids(self) -> BankMetadata:
        accounts = [a.account_id for a in self.accounts]
        targets = [t.target_id for t in self.targets]
        if len(accounts) != len(set(accounts)) or len(targets) != len(set(targets)):
            raise ValueError("duplicate account_id or target_id in bank_metadata")
        return self


class ValidationRequest(_Contract):
    """``POST /internal/validate`` body. Built by the gateway from its own records only."""

    request_id: Id
    conversation: ConversationSnapshot
    bundle: FrozenBundle
    bank_metadata: BankMetadata


# ── Model output and result ──────────────────────────────────────────


class EvidenceSpan(_Contract):
    """Character offsets (Python ``str`` indices, end exclusive) into a turn's text."""

    turn_id: str = Field(min_length=1, max_length=128)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class ValidationIssue(_Contract):
    leg_id: str | None = Field(max_length=128)
    field: IssueField
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    supported_value: str | None = Field(max_length=MAX_SUPPORTED_VALUE_CHARS)
    evidence: list[EvidenceSpan] = Field(max_length=MAX_EVIDENCE_PER_ISSUE)


class VerdictPayload(_Contract):
    """Exactly the JSON object the model must return. Every key is required."""

    verdict: Verdict
    issues: list[ValidationIssue] = Field(max_length=MAX_ISSUES)


class TokenUsage(_Contract):
    """Provider-reported token counts. ``None`` means unavailable, not zero."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ValidationResult(_Contract):
    """Validator response (HTTP 200) and the value ValidatorClient returns.

    ``status == "OK"`` carries a verdict and issues; ``status == "ERROR"`` carries a
    code and no verdict. ``input_hash`` is always the hash of the request checked.
    """

    status: Literal["OK", "ERROR"]
    verdict: Verdict | None = None
    issues: list[ValidationIssue] | None = Field(default=None, max_length=MAX_ISSUES)
    code: ErrorCode | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_hash: Hex64 | None = None
    input_hash: Hex64
    latency_ms: int | None = Field(default=None, ge=0)
    usage: TokenUsage | None = None

    @model_validator(mode="after")
    def _shape(self) -> ValidationResult:
        if self.status == "OK":
            if self.verdict is None or self.issues is None or self.code is not None:
                raise ValueError("OK result needs verdict and issues and no code")
            if self.model is None or self.prompt_hash is None or self.latency_ms is None:
                raise ValueError("OK result needs model, prompt_hash and latency_ms")
        elif self.code is None or self.verdict is not None or self.issues is not None:
            raise ValueError("ERROR result needs a code and no verdict or issues")
        return self

    @property
    def permits_confirmation(self) -> bool:
        """True only for an OK MATCH. Eligibility for the confirmation screen, nothing more."""
        return self.status == "OK" and self.verdict == "MATCH"


# ── Pure checks shared by both sides ─────────────────────────────────


def validation_input_hash(request: ValidationRequest) -> str:
    """SHA-256 (hex) of the canonical request bytes (shared/canonical.py rules)."""
    return hashlib.sha256(canonical_bytes(request)).hexdigest()


def _over_context_budget(turns: Sequence[ConversationTurn]) -> bool:
    if len(turns) > MAX_TURNS:
        return True
    if any(len(t.text) > MAX_TURN_CHARS for t in turns):
        return True
    return sum(len(t.text) for t in turns) > MAX_TOTAL_CHARS


def _conversation_is_consistent(turns: Sequence[ConversationTurn]) -> bool:
    seen: dict[str, ConversationTurn] = {}
    previous_seq = 0
    for turn in turns:
        if turn.turn_id in seen or turn.seq <= previous_seq:
            return False
        if turn.edits_turn_id is not None:
            edited = seen.get(turn.edits_turn_id)
            if edited is None or edited.role not in USER_ROLES:
                return False
        if turn.answers_question_id is not None:
            question = seen.get(turn.answers_question_id)
            if question is None or question.role != "app_question":
                return False
        seen[turn.turn_id] = turn
        previous_seq = turn.seq
    return any(t.role in USER_ROLES for t in turns)


def request_problem(request: ValidationRequest) -> ErrorCode | None:
    """Return the stable code for a request the validator must refuse, else None."""
    conversation = request.conversation
    if _over_context_budget(conversation.turns):
        return "CONTEXT_LIMIT_RESTATE"
    if not _conversation_is_consistent(conversation.turns):
        return "CONVERSATION_INVALID"
    bundle = request.bundle
    if (
        bundle.conversation_id != conversation.conversation_id
        or bundle.conversation_revision != conversation.revision
        or bundle.conversation_hash != conversation.conversation_hash
    ):
        return "BINDING_MISMATCH"
    accounts = {a.account_id for a in request.bank_metadata.accounts}
    targets = {t.target_id for t in request.bank_metadata.targets}
    for leg in bundle.legs:
        if leg.source_account_id not in accounts or leg.target_id not in targets:
            return "BANK_METADATA_INCOMPLETE"
    return None


def verdict_problem(
    request: ValidationRequest, verdict: Verdict, issues: Sequence[ValidationIssue]
) -> ErrorCode | None:
    """Check a verdict against the request. A passing check does not make it correct."""
    if verdict == "MATCH" and issues:
        return "OUTPUT_VERDICT_INCONSISTENT"
    if verdict == "MISMATCH" and not issues:
        return "OUTPUT_VERDICT_INCONSISTENT"
    leg_ids = {leg.leg_id for leg in request.bundle.legs}
    texts = {t.turn_id: t.text for t in request.conversation.turns}
    for issue in issues:
        if issue.leg_id is not None and issue.leg_id not in leg_ids:
            return "OUTPUT_UNKNOWN_LEG"
        for span in issue.evidence:
            text = texts.get(span.turn_id)
            if text is None or not 0 <= span.start < span.end <= len(text):
                return "OUTPUT_EVIDENCE_INVALID"
    return None


# ── Client ───────────────────────────────────────────────────────────


def _error(input_hash: str, code: ErrorCode) -> ValidationResult:
    return ValidationResult(status="ERROR", code=code, input_hash=input_hash)


def _code_from_error_body(response: httpx.Response) -> ErrorCode:
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return "VALIDATOR_HTTP_ERROR"
    return code if code in _REQUEST_ERROR_CODES else "VALIDATOR_HTTP_ERROR"


class ValidatorClient:
    """Gateway-side client for ``POST /internal/validate``.

    ``validate`` never raises for transport or validator failures; it returns an
    ERROR result instead. The caller binds the result to the bundle hash, conversation
    hash/revision, prompt hash and model before trusting it for anything.
    """

    def __init__(
        self,
        base_url: str,
        service_token: str | None,
        *,
        timeout_seconds: float = 40.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + VALIDATE_PATH
        self._token = service_token or ""
        self._timeout = timeout_seconds
        self._http = http_client

    def __repr__(self) -> str:  # never show the token
        return f"ValidatorClient(url={self._url!r})"

    def validate(self, request: ValidationRequest) -> ValidationResult:
        input_hash = validation_input_hash(request)
        if len(self._token) < 32:
            return _error(input_hash, "SERVICE_TOKEN_UNSET")
        problem = request_problem(request)
        if problem is not None:
            return _error(input_hash, problem)
        body = canonical_bytes(request)
        if len(body) > MAX_REQUEST_BYTES:
            return _error(input_hash, "CONTEXT_LIMIT_RESTATE")

        try:
            response = self._post(body)
        except httpx.TimeoutException:
            return _error(input_hash, "VALIDATOR_TIMEOUT")
        except httpx.HTTPError:
            return _error(input_hash, "VALIDATOR_UNAVAILABLE")

        if response.status_code != 200:
            return _error(input_hash, _code_from_error_body(response))
        try:
            result = ValidationResult.model_validate_json(response.content)
        except ValidationError:
            return _error(input_hash, "VALIDATOR_RESPONSE_INVALID")
        if result.input_hash != input_hash:
            return _error(input_hash, "INPUT_HASH_MISMATCH")
        if result.status == "OK":
            problem = verdict_problem(request, result.verdict, result.issues or [])
            if problem is not None:
                return ValidationResult(
                    status="ERROR",
                    code=problem,
                    model=result.model,
                    prompt_hash=result.prompt_hash,
                    input_hash=input_hash,
                    latency_ms=result.latency_ms,
                    usage=result.usage,
                )
        return result

    def _post(self, body: bytes) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if self._http is not None:
            return self._http.post(self._url, content=body, headers=headers, timeout=self._timeout)
        with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
            return client.post(self._url, content=body, headers=headers)
