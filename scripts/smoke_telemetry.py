#!/usr/bin/env python3
"""Smoke test: shared.telemetry end to end against Langfuse Cloud (T-08).

Builds one trace only through the safe helpers in ``shared.telemetry``:

    ingest (span, session + pseudonymous user)
      extract (span)
        extractor-llm-call (generation: model, prompt version/hash, usage, latency)
      policy (span, expected "blocked" outcome -> WARNING, not a crash)
      execution (span whose exception message carries canaries -> ERROR, no message)
    semantic_validation (second service instance, continued from the injected
                         ``traceparent`` -> same trace, parent = ingest)

Inputs/outputs/attributes/exception also carry canary secrets (fake API key, bearer
token, dcta_ token, OTP, full account number). After ``flush()`` the trace is read back
through ``GET /api/public/v2/observations`` and the script asserts the observations
exist with the expected structure and that no canary reached Langfuse.

Synthetic data only. Reads credentials from .env at runtime; never prints them.
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid

from shared.settings import load_agent_settings
from shared.telemetry import Telemetry, extract_trace_context, inject_trace_headers

EXPECTED = {
    "ingest",
    "extract",
    "extractor-llm-call",
    "policy",
    "execution",
    "semantic_validation",
}


def _make_trace(tel: Telemetry, validator: Telemetry, run: str, canaries: dict) -> str | None:
    prompt = "Extract the requested transfer as JSON. Synthetic smoke test."
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    with tel.workflow_span(
        "ingest",
        session_id=f"t08-smoke-{run}",
        user_pseudonym=tel.pseudonymize("smoke-user"),
        attributes={
            "conversation_id": f"conv-{run}",
            "channel": "text",
            "synthetic": True,
            "api_key": canaries["api_key"],  # not allowlisted: must be dropped
        },
        input={
            "text": "send 50 dollars to Mum from DBS Savings ending 4521",
            "Authorization": f"Bearer {canaries['bearer']}",
        },
    ) as root:
        trace_id = root.trace_id
        headers = inject_trace_headers()
        with tel.workflow_span("extract") as extract:
            with tel.generation(
                "extractor-llm-call",
                model="hy3",
                prompt_version="smoke-t08",
                prompt_hash=prompt_hash,
                provider="tokenhub",
                input=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": f"send 50 to Mum, my otp is {canaries['otp']}, "
                        f"account {canaries['account']}",
                    },
                ],
                latency_ms=812.0,
            ) as gen:
                gen.update(
                    output={
                        "action": "transfer",
                        "amount": "50.00",
                        "recipient_alias": "Mum",
                        "note": f"token={canaries['dcta']}",
                    },
                    usage={"prompt_tokens": 42, "completion_tokens": 17},
                )
            extract.set_output({"legs": 1, "status": "proposed"})
        with tel.workflow_span(
            "policy", attributes={"amount": "50.00", "currency": "SGD", "leg_count": 1}
        ) as policy:
            policy.set_status("blocked", reason_code="SMOKE_EXPECTED_BLOCK")
        try:
            with tel.workflow_span("execution"):
                raise RuntimeError(f"simulated failure {canaries['api_key']} {canaries['dcta']}")
        except RuntimeError:
            pass  # the caller still sees its own exception; telemetry recorded only the type

    # A second service continues the same trace from the propagated header.
    with validator.workflow_span(
        "semantic_validation", parent=extract_trace_context(headers)
    ) as validation:
        validation.set_attributes({"validator_verdict": "agree"})
    return trace_id


def _checks(observations: list[dict], trace_id: str, canaries: dict, user: str) -> list:
    by_name = {o.get("name"): o for o in observations}
    gen = by_name.get("extractor-llm-call", {})
    usage = gen.get("usageDetails") or {}
    text = json.dumps(observations)
    leaked = [k for k, v in canaries.items() if v in text]
    return [
        ("all six observations present", set(by_name) >= EXPECTED, sorted(by_name)),
        ("one trace id", {o.get("traceId") for o in observations} == {trace_id}, None),
        (
            "generation type + model",
            (gen.get("type"), gen.get("model")) == ("GENERATION", "hy3"),
            (gen.get("type"), gen.get("model")),
        ),
        ("usage recorded", (usage.get("input"), usage.get("output")) == (42, 17), usage),
        (
            "blocked is WARNING",
            (by_name.get("policy", {}).get("level"), by_name.get("policy", {}).get("statusMessage"))
            == ("WARNING", "blocked: SMOKE_EXPECTED_BLOCK"),
            None,
        ),
        (
            "error has type only",
            by_name.get("execution", {}).get("statusMessage") == "error: RuntimeError",
            by_name.get("execution", {}).get("statusMessage"),
        ),
        (
            "cross-service parent",
            by_name.get("semantic_validation", {}).get("parentObservationId")
            == by_name.get("ingest", {}).get("id"),
            None,
        ),
        (
            "pseudonymous user + session",
            by_name.get("ingest", {}).get("userId") == user
            and str(by_name.get("ingest", {}).get("sessionId", "")).startswith("t08-smoke-"),
            None,
        ),
        ("no canary in Langfuse", not leaked, leaked),
    ]


def main() -> int:
    print("=" * 60)
    print("SMOKE: shared.telemetry -> Langfuse Cloud (T-08)")
    print("=" * 60)
    settings = load_agent_settings()
    tel = Telemetry(settings, "smoke-agent")
    validator = Telemetry(settings, "smoke-validator")
    try:
        if tel.health()["state"] != "ok":
            print(f"[BLOCKER] telemetry health: {tel.health()}")
            return 1
        if not tel.check_backend():
            print(f"[FAILED] Langfuse auth_check: {tel.health()['last_error']}")
            return 1
        print("Auth check: OK (keys not printed)")

        run = uuid.uuid4().hex[:12]
        canaries = {
            "api_key": f"sk-smokecanary{run}ABCDEF",
            "bearer": f"smokeBearer{run}XYZ",
            "dcta": f"dcta_smokecanary{run}",
            "otp": str(100000 + int(run[:5], 16) % 900000),
            "account": "8" + str(int(run[:10], 16)).rjust(12, "7")[:12],
        }
        trace_id = _make_trace(tel, validator, run, canaries)
        tel.flush()
        health = tel.health()
        print(f"\nTrace ID: {trace_id}")
        print(f"Trace URL: {tel.trace_url(trace_id)}")
        print(f"Health after flush: {health}")
        if not trace_id or health["state"] != "ok":
            print("[FAILED] trace not created or export degraded")
            return 1

        print("\nReading back via GET /api/public/v2/observations (ingestion lag up to ~60 s)…")
        result = tel.read_back(trace_id, wait_s=120, poll_interval_s=5, min_count=len(EXPECTED))
        if result.error:
            print(f"[FAILED] {result.error}")
            return 1
        for obs in sorted(result.observations, key=lambda o: o.get("startTime") or ""):
            print(
                f"  {obs.get('type'):<10} {obs.get('name'):<22} level={obs.get('level')} "
                f"status={obs.get('statusMessage')!r}"
            )

        user = tel.pseudonymize("smoke-user")
        failures = 0
        print("\nChecks:")
        for label, ok, detail in _checks(result.observations, trace_id, canaries, user):
            failures += not ok
            suffix = "" if ok or detail is None else f"  ({detail})"
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
        print("\n" + ("ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED"))
        return 0 if not failures else 1
    finally:
        validator.shutdown()
        tel.shutdown()


if __name__ == "__main__":
    sys.exit(main())
