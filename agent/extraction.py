"""The one agent-side model call: copy instruction words out of the conversation.

The model returns quoted words only (strict JSON, see ``prompts/extractor.md``). It
never returns IDs, offsets, display names or amounts in numeric form, and it has no
tools. Malformed, empty or schema-invalid output is an error: no proposal is made.
Reasoning tokens (``reasoning_content`` on hy3) are never read or forwarded.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.prompt import ExtractorPrompt
from agent.turns import VisibleTurn
from shared.deadline import DeadlineExpired, bounded_call, current_deadline, remaining_timeout
from shared.settings import ExtractReasoningEffort, extractor_reasoning_body

__all__ = [
    "ExtractedIntent",
    "ExtractionResult",
    "ExtractorLLM",
    "ExtractorOutput",
    "IntentAction",
    "LLMReply",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_TURNS",
    "OpenAICompatibleExtractor",
    "build_extraction_input",
    "extract_intents",
    "parse_extractor_output",
]

_EXTRACT_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="extractor-llm")

#: Bounds on what the extractor reads. Over the bound the agent asks for a restatement
#: (via an error status) instead of silently truncating history.
MAX_CONTEXT_TURNS = 40
MAX_CONTEXT_CHARS = 8000
MAX_QUOTE_LEN = 200
MAX_INTENTS = 10

UnclearField = Literal["amount", "recipient", "source", "reference", "relative_to", "action"]
GlobalUnclear = Literal[
    "intent", "amount", "recipient", "source", "reference", "relative_to", "action"
]


class IntentAction(StrEnum):
    transfer = "transfer"
    pay_bill = "pay_bill"
    pay_card = "pay_card"
    buy_equity = "buy_equity"
    unsupported = "unsupported"


class ExtractedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: IntentAction
    amount_quote: str | None = Field(default=None, max_length=MAX_QUOTE_LEN)
    recipient_quote: str | None = Field(default=None, max_length=MAX_QUOTE_LEN)
    source_quote: str | None = Field(default=None, max_length=MAX_QUOTE_LEN)
    reference_quote: str | None = Field(default=None, max_length=MAX_QUOTE_LEN)
    relative_to_quote: str | None = Field(default=None, max_length=MAX_QUOTE_LEN)
    unclear: list[UnclearField] = Field(default_factory=list, max_length=6)

    @field_validator(
        "amount_quote",
        "recipient_quote",
        "source_quote",
        "reference_quote",
        "relative_to_quote",
        mode="after",
    )
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v if v is not None and v.strip() else None


class ExtractorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intents: list[ExtractedIntent] = Field(default_factory=list, max_length=MAX_INTENTS)
    unclear: list[GlobalUnclear] = Field(default_factory=list, max_length=7)


# ── Model access (injectable) ─────────────────────────────────────────


@dataclass(frozen=True)
class LLMReply:
    content: str | None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


class ExtractorLLM(Protocol):
    model: str

    def complete(self, system_prompt: str, user_content: str) -> LLMReply: ...


class OpenAICompatibleExtractor:
    """OpenAI-compatible chat call (TokenHub ``hy3`` by default) with JSON output."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int,
        provider: str | None = None,
        reasoning_effort: ExtractReasoningEffort | None = None,
        timeout: float = 40.0,
        max_retries: int = 0,
        client: Any = None,
    ) -> None:
        self._extra_body = extractor_reasoning_body(provider, model, reasoning_effort)
        if max_tokens <= 0:
            raise ValueError("extractor max_tokens must be positive")
        self._owns_client = client is None
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
            )
        self._client = client
        self._timeout = timeout
        self.model = model
        self.provider = provider
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_settings(cls, settings: Any) -> OpenAICompatibleExtractor:
        if not settings.llm_enabled:
            raise RuntimeError("extractor model credentials are not configured")
        return cls(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.extract_model,
            max_tokens=settings.extract_max_tokens,
            provider=settings.llm_provider,
            reasoning_effort=settings.extract_reasoning_effort,
            timeout=settings.extract_timeout_seconds,
            max_retries=settings.extract_max_retries,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, system_prompt: str, user_content: str) -> LLMReply:
        client = self._client
        options: dict[str, Any] = {}
        if current_deadline.get() is not None:
            # SDK backoff/retries cannot each receive a fresh request timeout.
            client = client.with_options(max_retries=0)
            options["timeout"] = remaining_timeout(self._timeout)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens,
            temperature=0,
            **({"extra_body": self._extra_body} if self._extra_body else {}),
            **options,
        )
        choice = resp.choices[0]
        usage = None
        if resp.usage is not None:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
            details = getattr(resp.usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
                usage["reasoning_tokens"] = reasoning_tokens
        # Only the final answer is used; reasoning_content is deliberately ignored.
        return LLMReply(
            content=choice.message.content, finish_reason=choice.finish_reason, usage=usage
        )


# ── Input / output ────────────────────────────────────────────────────


def build_extraction_input(turns: Sequence[VisibleTurn]) -> str:
    """The conversation as a JSON data object: current turns only, in order."""
    lines = [
        {"n": i, "speaker": "user" if t.is_user else "app", "text": t.text}
        for i, t in enumerate(turns, start=1)
    ]
    return json.dumps({"conversation": lines}, ensure_ascii=False)


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_extractor_output(content: str | None) -> ExtractorOutput | str:
    """Strictly parse the model's reply. Returns an error code string on failure."""
    if content is None or not content.strip():
        return "EXTRACTION_EMPTY"
    text = content.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "EXTRACTION_MALFORMED"
    if not isinstance(data, dict):
        return "EXTRACTION_MALFORMED"
    try:
        return ExtractorOutput.model_validate(data)
    except ValidationError:
        return "EXTRACTION_SCHEMA_INVALID"


@dataclass(frozen=True)
class ExtractionResult:
    output: ExtractorOutput | None
    error: str | None
    model: str
    latency_ms: int
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    input_chars: int = 0
    reasoning_effort: ExtractReasoningEffort | None = None
    max_tokens: int | None = None

    def audit_payload(self, prompt: ExtractorPrompt) -> dict[str, Any]:
        """Small, secret-free summary for the agent.extract audit event."""
        return {
            "prompt_version": prompt.version,
            "prompt_sha256": prompt.sha256,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "input_chars": self.input_chars,
            "intents": len(self.output.intents) if self.output else 0,
            "outcome": self.error or "ok",
        }


def extract_intents(
    llm: ExtractorLLM,
    prompt: ExtractorPrompt,
    turns: Sequence[VisibleTurn],
    *,
    before_call: Callable[[], None] | None = None,
) -> ExtractionResult:
    """Run the extractor once. Provider failures and bad output become error codes."""
    user_content = build_extraction_input(turns)
    settings = {
        "reasoning_effort": getattr(llm, "reasoning_effort", None),
        "max_tokens": getattr(llm, "max_tokens", None),
    }
    started = time.monotonic()
    try:
        budget = current_deadline.get()

        def call() -> LLMReply:
            if before_call is not None:
                before_call()
            return llm.complete(prompt.text, user_content)

        reply = bounded_call(_EXTRACT_EXECUTOR, budget, call) if budget is not None else call()
    except DeadlineExpired:
        return ExtractionResult(
            None,
            "EXTRACTION_TIMEOUT",
            llm.model,
            _ms(started),
            input_chars=len(user_content),
            **settings,
        )
    except Exception:  # noqa: BLE001 - any provider failure fails closed
        return ExtractionResult(
            None,
            "EXTRACTION_FAILED",
            llm.model,
            _ms(started),
            input_chars=len(user_content),
            **settings,
        )
    parsed = (
        "EXTRACTION_TRUNCATED"
        if reply.finish_reason == "length"
        else parse_extractor_output(reply.content)
    )
    output, error = (None, parsed) if isinstance(parsed, str) else (parsed, None)
    return ExtractionResult(
        output,
        error,
        llm.model,
        _ms(started),
        finish_reason=reply.finish_reason,
        usage=reply.usage,
        input_chars=len(user_content),
        **settings,
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
