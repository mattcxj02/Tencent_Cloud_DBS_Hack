#!/usr/bin/env python3
"""Smoke test: LLM connectivity and JSON output for the extractor and validator roles.

Each role uses its own configured provider (TokenHub or DeepSeek), model and token budget.
For each role: list the provider's models, check the configured model is listed, then
request a JSON object and confirm it parses. Credentials are read at runtime only and
never printed. A missing credential is reported as a blocker, not a pass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from openai import OpenAI

from shared.settings import LLMSettings, load_agent_settings, load_validator_settings

SYSTEM = "You are a helpful assistant. Respond only with JSON."


@dataclass
class Role:
    name: str
    settings: LLMSettings
    model: str
    max_tokens: int
    prompt: str


def _check_role(role: Role) -> bool:
    s = role.settings
    print(f"\n--- {role.name}: provider={s.llm_provider} model={role.model} ---")
    print(f"Base URL: {s.llm_base_url}")
    if not s.llm_enabled:
        print(f"[BLOCKER] No API key configured for provider '{s.llm_provider}'.")
        return False

    client = OpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url, timeout=90)
    try:
        ids = sorted(m.id for m in client.models.list().data)
        listed = role.model in ids
        print(f"GET /models: {len(ids)} models; configured model listed: {listed}")
    except Exception as exc:  # noqa: BLE001
        print(f"GET /models: FAILED {type(exc).__name__}: {str(exc)[:200]}")

    started = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=role.model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": role.prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=role.max_tokens,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAILED] {type(exc).__name__}: {str(exc)[:300]}")
        return False

    elapsed = time.monotonic() - started
    choice = resp.choices[0]
    raw = choice.message.content or ""
    usage = resp.usage.completion_tokens if resp.usage else "unavailable"
    print(f"finish_reason={choice.finish_reason} completion_tokens={usage} latency={elapsed:.1f}s")
    print(f"Output: {raw[:300]}")
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        print(
            "[FAILED] Output is not valid JSON (empty output usually means max_tokens is too low)."
        )
        return False
    return True


def main() -> None:
    print("=" * 60)
    print("SMOKE: LLM connectivity + JSON output (extractor and validator)")
    print("=" * 60)

    agent = load_agent_settings()
    validator = load_validator_settings()
    roles = [
        Role(
            "Extractor",
            agent,
            agent.extract_model,
            agent.extract_max_tokens,
            'Extract a transfer from: "Send fifty dollars to John". '
            'Return JSON: {"action": "transfer", "amount": "50.00", "recipient": "John"}',
        ),
        Role(
            "Validator",
            validator,
            validator.validate_model,
            validator.validate_max_tokens,
            'Compare the request "Send 50 SGD to John" against a draft transfer of '
            '50.00 SGD to John. Return JSON: {"verdict": "MATCH", "issues": []}',
        ),
    ]
    results = {role.name: _check_role(role) for role in roles}

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for role in roles:
        status = "OK" if results[role.name] else "FAILED"
        print(f"{role.name} ({role.settings.llm_provider}/{role.model}): {status}")


if __name__ == "__main__":
    main()
