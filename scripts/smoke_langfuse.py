#!/usr/bin/env python3
"""Smoke test: Langfuse backend connectivity.

Creates one trace with one span and one generation-type observation using
synthetic data, flushes, and prints the trace ID/URL. No secrets are printed.

Uses the current Langfuse Python SDK. The exact API surface is checked at
runtime to match the installed version (do not mix v2/v3 examples).
"""

from __future__ import annotations

import sys

from shared.settings import load_agent_settings


def main() -> None:
    print("=" * 60)
    print("SMOKE: Langfuse backend connectivity")
    print("=" * 60)

    # Use agent settings (agent needs Langfuse). Any service's Langfuse config works.
    settings = load_agent_settings()

    if not settings.langfuse_enabled:
        print("\n[BLOCKER] Langfuse credentials not configured.")
        print("  Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL in .env.")
        sys.exit(1)

    print(f"\nBase URL: {settings.langfuse_base_url}")
    print("Public key: …REDACTED")
    print("Secret key: …REDACTED")

    # ── Determine SDK version and API ──
    try:
        import langfuse

        sdk_version = getattr(langfuse, "__version__", "unknown")
        print(f"Langfuse SDK version: {sdk_version}")
    except ImportError:
        print("[BLOCKER] langfuse package not installed.")
        print("  Run: uv pip install langfuse")
        sys.exit(1)

    # ── Initialize client (Langfuse v4, OpenTelemetry-based API) ──
    from langfuse import Langfuse, propagate_attributes

    lf = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
    )
    if not lf.auth_check():
        print("[FAILED] Langfuse auth_check() returned False (keys or base URL wrong).")
        sys.exit(1)
    print("Auth check: OK")

    # ── Create trace + span + generation ──
    print("\n--- Create trace with span + generation ---")
    trace_id: str | None = None
    trace_url: str | None = None
    error: str | None = None

    try:
        with lf.start_as_current_observation(
            name="dcta-smoke-test", as_type="span", input={"text": "send fifty dollars to John"}
        ):
            with (
                propagate_attributes(
                    user_id="smoke-test-user",
                    session_id="smoke-test-session",
                    metadata={"source": "T-00-smoke", "synthetic": "true"},
                ),
                lf.start_as_current_observation(name="extract", as_type="span"),
                lf.start_as_current_observation(
                    name="extractor-llm-call",
                    as_type="generation",
                    model="hy3",
                    input=[{"role": "user", "content": "send fifty dollars to John"}],
                    output={"action": "transfer", "amount": "50.00", "recipient": "John"},
                    metadata={"prompt_version": "smoke", "synthetic": "true"},
                ),
            ):
                pass
            trace_id = lf.get_current_trace_id()
        trace_url = lf.get_trace_url(trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        lf.flush()

    # ── Report ──
    print("\n--- Result ---")
    if trace_id:
        print(f"Trace ID: {trace_id}")
        print(f"Trace URL: {trace_url}")
    else:
        print(f"[FAILED] No trace created: {error}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Langfuse SDK: {sdk_version}")
    if trace_id:
        print(f"Trace: OK (ID: {trace_id})")
        print(f"URL: {trace_url}")
    else:
        print(f"Trace: FAILED ({error})")


if __name__ == "__main__":
    main()
