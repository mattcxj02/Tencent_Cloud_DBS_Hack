"""Fake models for the ``--fake`` dry run only (plumbing check, never a measurement).

- :class:`OracleExtractor` returns the case's hand-written ``oracle_extraction``: the
  words a perfect extractor would quote. Running it through the real agent graph and
  gateway proves each fixture's expected outcome is reachable by the deterministic code
  (amount arithmetic, equity quantities, dependencies, citations, clarifications).
- :class:`OracleVerdictLLM` returns whatever verdict the runner sets before each call
  (MATCH for correct drafts, MISMATCH for incorrect ones). Fake-mode validator numbers
  are therefore perfect by construction and must never be reported as results.
"""

from __future__ import annotations

import json
from typing import Any

from agent.extraction import LLMReply as ExtractorReply
from validator.llm import LLMReply as ValidatorReply

__all__ = ["OracleExtractor", "OracleVerdictLLM"]


class OracleExtractor:
    model = "fake-oracle-extractor"

    def __init__(self, extraction: dict[str, Any]) -> None:
        self._content = json.dumps(extraction)

    def complete(self, system_prompt: str, user_content: str) -> ExtractorReply:
        return ExtractorReply(content=self._content, finish_reason="stop", usage=None)


class OracleVerdictLLM:
    """Validator fake: replies with the verdict set by :meth:`expect`."""

    def __init__(self) -> None:
        self._verdict = "MATCH"
        self._leg_id: str | None = None
        self.calls = 0

    def expect(self, verdict: str, leg_id: str | None = None) -> None:
        self._verdict, self._leg_id = verdict, leg_id

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> ValidatorReply:
        self.calls += 1
        issues = []
        if self._verdict != "MATCH":
            issues = [
                {
                    "leg_id": self._leg_id,
                    "field": "other",
                    "reason": "fake oracle verdict (dry run)",
                    "supported_value": None,
                    "evidence": [],
                }
            ]
        body = {"verdict": self._verdict, "issues": issues}
        return ValidatorReply(content=json.dumps(body), finish_reason="stop", usage=None)
