"""Model client for the validator: one chat completion, JSON mode, no tools.

``LLMClient`` is the injectable interface (tests use fakes). ``OpenAICompatibleLLM``
calls the configured provider (DeepSeek or TokenHub) with temperature 0 and
``response_format={"type": "json_object"}``. No ``tools`` are sent. Reasoning models
may return ``reasoning_content``; it is ignored and never logged. Only ``content``
is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shared.clients.validator import TokenUsage
from shared.settings import ValidatorSettings

__all__ = ["LLMReply", "LLMClient", "LLMTimeoutError", "OpenAICompatibleLLM", "build_llm"]


@dataclass(frozen=True)
class LLMReply:
    content: str | None
    finish_reason: str | None
    has_tool_calls: bool = False
    usage: TokenUsage | None = None


class LLMTimeoutError(Exception):
    """The provider call exceeded its timeout."""


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> LLMReply:
        """Return the model's reply or raise. Any exception is treated as an ERROR."""
        ...


def _usage(raw: Any) -> TokenUsage | None:
    if raw is None:
        return None

    def count(name: str) -> int | None:
        value = getattr(raw, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return TokenUsage(
        prompt_tokens=count("prompt_tokens"),
        completion_tokens=count("completion_tokens"),
        total_tokens=count("total_tokens"),
    )


class OpenAICompatibleLLM:
    """OpenAI-compatible chat client. ``client`` can be injected for tests."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int,
        client: Any = None,
    ) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> LLMReply:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=self._max_tokens,
                temperature=0,
                timeout=timeout_seconds,
            )
        except openai.APITimeoutError:
            raise LLMTimeoutError("provider call timed out") from None

        if not response.choices:
            return LLMReply(content=None, finish_reason=None, usage=_usage(response.usage))
        choice = response.choices[0]
        message = choice.message
        return LLMReply(
            content=message.content,
            finish_reason=choice.finish_reason,
            has_tool_calls=bool(getattr(message, "tool_calls", None)),
            usage=_usage(response.usage),
        )


def build_llm(settings: ValidatorSettings) -> OpenAICompatibleLLM | None:
    """Real client from settings, or None when the provider key is missing."""
    if not settings.llm_enabled or settings.llm_api_key is None:
        return None
    return OpenAICompatibleLLM(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.validate_model,
        max_tokens=settings.validate_max_tokens,
    )
