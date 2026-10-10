"""T-08 shared telemetry: secret canaries, allowlist, health, propagation, usage/cost.

Every test uses ``Telemetry.for_testing`` (unique fake key, unroutable URL, in-memory
exporter) so nothing leaves the process. Assertions inspect exactly what would be sent.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shared.settings import LangfuseSettings
from shared.telemetry import (
    ModelPrice,
    Telemetry,
    async_httpx_request_hook,
    extract_trace_context,
    httpx_request_hook,
    inject_trace_headers,
    parse_traceparent,
    pseudonymize,
    redact_text,
    sanitize_attributes,
)

# ── helpers ──────────────────────────────────────────────────────────

CANARIES = {
    "api_key": "sk-canaryKEY0123456789abcdefXYZ",
    "langfuse_key": "sk-lf-canary-0000-1111-2222",
    "bearer": "canaryBearer0123456789Tok",
    "dcta": "dcta_canaryDelegation.c2lnbmF0dXJl",
    "otp": "482913",
    "signature": "MEUCIQDcanarySigABCdef0123456789",
    "client_data": "eyJjYW5hcnkiOiJjbGllbnREYXRhSlNPTiJ9",
    "auth_data": "SZYN5YgOcanaryAuthData0123",
    "account": "0123456789012",
    "audio": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA9canaryAudio0123",
    "private_key": "MIIEvQIBADANcanaryPRIVATEkey",
    "cookie": "canarySessionCookieValue42",
}


def _payload() -> dict:
    """A request-shaped payload with every kind of secret the plan forbids."""
    return {
        "text": "send 50 to Mum from DBS Savings ending 4521",
        "headers": {"Authorization": f"Bearer {CANARIES['bearer']}"},
        "Authorization": f"Bearer {CANARIES['bearer']}",
        "Cookie": f"dcta_session={CANARIES['cookie']}",
        "api_key": CANARIES["api_key"],
        "otp": CANARIES["otp"],
        "delegation_token": CANARIES["dcta"],
        "assertion": {
            "response": {
                "clientDataJSON": CANARIES["client_data"],
                "authenticatorData": CANARIES["auth_data"],
                "signature": CANARIES["signature"],
            }
        },
        "clientDataJSON": CANARIES["client_data"],
        "account_number": CANARIES["account"],
        "audio_b64": CANARIES["audio"],
        "clip": f"data:audio/wav;base64,{CANARIES['audio']}",
        "raw": b"RIFF\x00\x00WAVE",
        "notes": [
            f"Authorization: Bearer {CANARIES['bearer']}",
            f"your one-time code is {CANARIES['otp']}",
            f"pay account {CANARIES['account']} now",
            f"token={CANARIES['dcta']} key {CANARIES['api_key']} lf {CANARIES['langfuse_key']}",
            f"-----BEGIN PRIVATE KEY-----\n{CANARIES['private_key']}\n-----END PRIVATE KEY-----",
            f"Cookie: dcta_session={CANARIES['cookie']}; theme=dark",
            f"audio {CANARIES['audio']}",
        ],
    }


def _exported(tel: Telemetry) -> list:
    tel.flush()
    return list(tel.exporter.get_finished_spans())


def _dump(spans: list) -> str:
    rows = []
    for s in spans:
        rows.append(
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in s.events],
                "status": s.status.description,
                "resource": dict(s.resource.attributes),
                "links": [str(link.context) for link in s.links],
            }
        )
    return json.dumps(rows, default=str)


def _assert_no_canary(text: str) -> None:
    leaked = [name for name, value in CANARIES.items() if value in text]
    assert not leaked, f"canaries reached the exporter: {leaked}"


def _by_name(spans: list, name: str):
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r}: {[s.name for s in spans]}"
    return matches[0]


def _meta(span, key: str):
    return span.attributes.get(f"langfuse.observation.metadata.{key}")


@pytest.fixture
def tel():
    t = Telemetry.for_testing("agent")
    yield t
    t.shutdown()


# ── secret canaries ──────────────────────────────────────────────────


def test_canaries_never_reach_exporter_but_allowlisted_fields_do(tel: Telemetry) -> None:
    attributes = {
        # allowlisted, well-formed: exported
        "bundle_id": "bundle-123",
        "amount": "50.00",
        "currency": "SGD",
        "recipient_alias": "Mum",
        "account_masked": "ending 4521",
        "operation_type": "transfer",
        # not allowlisted: dropped even though harmless-looking
        "user_id": "user-matthew",
        "api_key": CANARIES["api_key"],
        "authorization": f"Bearer {CANARIES['bearer']}",
        # allowlisted key, secret value: rejected by the kind check
        "conversation_id": CANARIES["dcta"],
        "request_id": CANARIES["account"],
        "audit_seq": int(CANARIES["account"]),
    }
    with tel.workflow_span(
        "extract",
        session_id="sess-abc",
        user_pseudonym="user-matthew",
        attributes=attributes,
        input=_payload(),
    ) as span:
        with tel.generation(
            "extractor-llm-call",
            model="hy3",
            prompt_version="extract-v1",
            prompt_hash="ab" * 32,
            input=[{"role": "user", "content": f"send 50, my otp is {CANARIES['otp']}"}],
            usage={"prompt_tokens": 12, "completion_tokens": 7},
        ) as gen:
            gen.update(output=_payload())
        span.set_output(_payload())
        span.set_attributes({"client_secret": CANARIES["api_key"], "leg_count": 1})
        tel.event("awaiting_approval", attributes={"otp": CANARIES["otp"], "attempt": 1})

    spans = _exported(tel)
    assert {s.name for s in spans} == {"extract", "extractor-llm-call", "awaiting_approval"}
    _assert_no_canary(_dump(spans))

    extract = _by_name(spans, "extract")
    assert _meta(extract, "bundle_id") == "bundle-123"
    assert _meta(extract, "amount") == "50.00"
    assert _meta(extract, "recipient_alias") == "Mum"
    assert _meta(extract, "account_masked") == "ending 4521"
    assert _meta(extract, "leg_count") == 1
    assert _meta(extract, "conversation_id") is None
    assert _meta(extract, "request_id") is None
    assert _meta(extract, "audit_seq") is None
    assert _meta(extract, "dropped_attribute_count") == 7
    assert extract.attributes["user.id"] == pseudonymize("user-matthew")
    assert extract.attributes["session.id"] == "sess-abc"
    exported_input = extract.attributes["langfuse.observation.input"]
    assert "ending 4521" in exported_input  # masked suffix survives
    assert "ending 9012" in exported_input  # full number reduced to its last four digits
    assert "user-matthew" not in _dump(spans)


def test_canaries_in_exception_paths_are_not_exported(tel: Telemetry) -> None:
    secret_message = " ".join(CANARIES.values())
    with pytest.raises(ValueError) as caught, tel.workflow_span("policy", input={"text": "ok"}):
        raise ValueError(secret_message)
    assert str(caught.value) == secret_message  # the caller's exception is untouched

    with (
        pytest.raises(RuntimeError),
        tel.generation(
            "validator-llm-call",
            model="deepseek-flash",
            prompt_version="v1",
            prompt_hash="cd" * 32,
            input={"q": "x"},
        ),
    ):
        raise RuntimeError(f"provider said Authorization: Bearer {CANARIES['bearer']}")

    spans = _exported(tel)
    _assert_no_canary(_dump(spans))
    policy = _by_name(spans, "policy")
    assert policy.events == ()
    assert policy.attributes["langfuse.observation.level"] == "ERROR"
    assert policy.attributes["langfuse.observation.status_message"] == "error: ValueError"
    assert _meta(policy, "error_type") == "ValueError"


def test_direct_otel_writes_and_events_are_dropped_at_export(tel: Telemetry) -> None:
    """Defence in depth: attributes/events set around the helpers never leave."""
    with tel.workflow_span("execution"):
        raw = otel_trace.get_current_span()
        raw.set_attribute("http.request.header.authorization", f"Bearer {CANARIES['bearer']}")
        raw.set_attribute("langfuse.observation.metadata.secret_blob", CANARIES["api_key"])
        raw.set_attribute("langfuse.observation.metadata.amount", f"otp {CANARIES['otp']}")
        raw.add_event("debug", {"token": CANARIES["dcta"]})
        raw.record_exception(ValueError(CANARIES["signature"]))
    spans = _exported(tel)
    _assert_no_canary(_dump(spans))
    span = _by_name(spans, "execution")
    assert span.events == ()
    assert "http.request.header.authorization" not in span.attributes


def test_session_and_model_names_cannot_carry_secrets(tel: Telemetry) -> None:
    with (
        tel.workflow_span(CANARIES["dcta"], session_id=CANARIES["dcta"]),
        tel.generation("g", model=CANARIES["api_key"], prompt_version="v1", prompt_hash="ef" * 32),
    ):
        pass
    spans = _exported(tel)
    _assert_no_canary(_dump(spans))
    assert {s.name for s in spans} == {"unnamed", "g"}
    assert all(s.attributes["session.id"].startswith("s_") for s in spans)


# ── statuses and span lifecycle ──────────────────────────────────────


class _Blocked(Exception):
    telemetry_status = "blocked"
    reason_code = "MANDATE_DAILY_EXCEEDED"


class _HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("detail")
        self.status_code = status_code


def test_expected_outcomes_are_levels_not_crashes(tel: Telemetry) -> None:
    with tel.workflow_span("clarify") as span:
        span.set_status("clarification", reason_code="AMBIGUOUS_PAYEE")
    with tel.workflow_span("policy") as span:
        span.set_status("blocked", reason_code="HARD_LIMIT")
    with pytest.raises(_Blocked), tel.workflow_span("approval"):
        raise _Blocked()
    with pytest.raises(_HTTPError), tel.workflow_span("challenge"):
        raise _HTTPError(403)
    with pytest.raises(TimeoutError), tel.workflow_span("semantic_validation"):
        raise TimeoutError()
    with tel.workflow_span("receipt") as span:
        span.set_status("not-a-status", reason_code="123456")  # numeric reason rejected

    spans = {s.name: s for s in _exported(tel)}
    level = "langfuse.observation.level"
    message = "langfuse.observation.status_message"
    assert spans["clarify"].attributes[level] == "DEFAULT"
    assert spans["clarify"].attributes[message] == "clarification: AMBIGUOUS_PAYEE"
    assert spans["policy"].attributes[level] == "WARNING"
    assert spans["approval"].attributes[message] == "blocked: MANDATE_DAILY_EXCEEDED"
    assert spans["challenge"].attributes[message] == "rejected: HTTP_403"
    assert spans["semantic_validation"].attributes[message] == "timeout"
    assert spans["receipt"].attributes[message] == "unknown"
    assert "123456" not in _dump(list(spans.values()))
    assert all(s.attributes.get(level) != "ERROR" for s in spans.values())
    assert _meta(spans["policy"], "stage") == "policy"


class _CodedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize(
    "code, exported", [("DRAFT_NOT_FOUND", "DRAFT_NOT_FOUND"), ("AB12CD", None), ("482913", None)]
)
def test_exception_codes_must_look_like_reason_codes(tel: Telemetry, code, exported) -> None:
    with pytest.raises(_CodedError), tel.workflow_span("step_up"):
        raise _CodedError(code)
    (span,) = _exported(tel)
    assert _meta(span, "reason_code") == exported
    if exported is None:
        assert code not in _dump([span])


def test_spans_are_closed_when_the_block_exits(tel: Telemetry) -> None:
    with tel.workflow_span("approval") as span:
        assert Telemetry.current_trace_id() == span.trace_id
    assert Telemetry.current_trace_id() is None  # nothing left open while a person decides
    (exported,) = _exported(tel)
    assert exported.end_time is not None


# ── health: disabled, degraded, recovery ─────────────────────────────


def test_missing_keys_mean_disabled_health_and_noop_spans() -> None:
    for settings in (
        None,
        LangfuseSettings.model_construct(
            langfuse_public_key=None, langfuse_secret_key=None, langfuse_base_url=None
        ),
        LangfuseSettings.model_construct(
            langfuse_public_key="pk-lf-x", langfuse_secret_key=None, langfuse_base_url=None
        ),
    ):
        tel = Telemetry(settings, "gateway")
        health = tel.health()
        assert health["state"] == "disabled"
        assert "not configured" in health["last_error"]
        with tel.workflow_span("ingest", session_id="s") as span:
            with tel.generation("g", model="hy3", prompt_version="v", prompt_hash="a" * 64) as g:
                g.update(output={"x": 1})
            span.set_status("ok")
            result = "business result"
        assert result == "business result"
        assert span.trace_id is None
        assert tel.trace_url("a" * 32) is None
        assert tel.check_backend() is False
        assert tel.read_back("a" * 32).ok is False
        tel.flush()
        tel.shutdown()


class _FlakyExporter(SpanExporter):
    def __init__(self, *, raise_error: bool = False) -> None:
        self.fail = True
        self.raise_error = raise_error
        self.inner = InMemorySpanExporter()

    def export(self, spans):
        if self.fail:
            if self.raise_error:
                raise ConnectionError("langfuse down")
            return SpanExportResult.FAILURE
        return self.inner.export(spans)

    def shutdown(self) -> None:
        self.inner.shutdown()


@pytest.mark.parametrize("raise_error", [False, True])
def test_export_failure_degrades_health_and_recovers_from_buffer(raise_error: bool) -> None:
    flaky = _FlakyExporter(raise_error=raise_error)
    tel = Telemetry.for_testing("gateway", exporter=flaky)
    try:
        with tel.workflow_span("policy") as span:
            span.set_status("blocked", reason_code="HARD_LIMIT")
            decision = "blocked"
        with tel.workflow_span("receipt"):
            pass
        tel.flush()
        assert decision == "blocked"  # the business outcome is whatever the caller decided
        health = tel.health()
        assert health["state"] == "degraded"
        assert health["pending"] == 2
        assert "export" in health["last_error"].lower()

        flaky.fail = False  # backend restored
        assert tel.reexport_pending() == 2
        assert {s.name for s in flaky.inner.get_finished_spans()} == {"policy", "receipt"}
        health = tel.health()
        assert health["state"] == "ok"
        assert health["pending"] == 0
        assert health["dropped"] == 0
    finally:
        tel.shutdown()


def test_export_buffer_is_bounded_and_counts_drops() -> None:
    flaky = _FlakyExporter()
    tel = Telemetry.for_testing("agent", exporter=flaky, max_pending_spans=2)
    try:
        for _ in range(3):
            with tel.workflow_span("extract"):
                pass
            tel.flush()
        health = tel.health()
        assert health["pending"] == 2
        assert health["dropped"] == 1
        assert health["state"] == "degraded"
    finally:
        tel.shutdown()


def test_internal_telemetry_errors_never_reach_the_caller(tel: Telemetry, monkeypatch) -> None:
    client = tel._backend.client

    def boom(*args, **kwargs):
        raise RuntimeError("sdk broke")

    monkeypatch.setattr(client, "start_as_current_observation", boom)
    with tel.workflow_span("execution") as span:
        span.set_status("ok")
        result = 42
    assert result == 42
    assert span.trace_id is None
    health = tel.health()
    assert health["state"] == "degraded"
    assert health["dropped"] == 1
    assert "span start" in health["last_error"]


def test_shutdown_reports_disabled() -> None:
    tel = Telemetry.for_testing("validator")
    tel.shutdown()
    assert tel.health()["state"] == "disabled"
    with tel.workflow_span("semantic_validation") as span:
        pass
    assert span.trace_id is None


# ── usage and cost ───────────────────────────────────────────────────


def test_generation_records_model_prompt_hash_usage_and_unavailable_cost(tel: Telemetry) -> None:
    with tel.generation(
        "extractor-llm-call",
        model="hy3",
        prompt_version="extract-v3",
        prompt_hash="0123456789abcdef" * 4,
        provider="tokenhub",
        model_parameters={"temperature": 0, "max_tokens": 2048, "api_key": "x"},
        input=[{"role": "user", "content": "pay Mum 50"}],
        latency_ms=812.5,
    ) as gen:
        gen.update(
            output={"action": "transfer"},
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 300,
                "completion_tokens_details": {"reasoning_tokens": 250},
            },
        )
    (span,) = _exported(tel)
    attrs = span.attributes
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "hy3"
    assert json.loads(attrs["langfuse.observation.model.parameters"]) == {
        "temperature": 0,
        "max_tokens": 2048,
    }
    assert _meta(span, "prompt_version") == "extract-v3"
    assert _meta(span, "prompt_hash") == "0123456789abcdef" * 4
    assert _meta(span, "provider") == "tokenhub"
    assert json.loads(_meta(span, "latency_ms")) == 812.5
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 120,
        "output": 300,
        "output_reasoning": 250,
        "total": 420,
    }
    assert _meta(span, "usage_status") == "reported"
    assert "langfuse.observation.cost_details" not in attrs  # never an invented zero
    assert _meta(span, "cost_status") == "unavailable"
    assert _meta(span, "cost_unavailable_reason") == "no_configured_price"


def test_cost_only_when_price_configured() -> None:
    tel = Telemetry.for_testing(
        "validator",
        prices={"deepseek-flash": ModelPrice(Decimal("0.5"), Decimal("2.0"))},
    )
    try:
        with tel.generation(
            "priced",
            model="deepseek-flash",
            prompt_version="v",
            prompt_hash="a" * 64,
            usage={"prompt_tokens": 1000, "completion_tokens": 500},
        ):
            pass
        with tel.generation(
            "no-usage", model="deepseek-flash", prompt_version="v", prompt_hash="a" * 64
        ):
            pass
        spans = {s.name: s for s in _exported(tel)}
        cost = json.loads(spans["priced"].attributes["langfuse.observation.cost_details"])
        assert cost == pytest.approx({"input": 0.0005, "output": 0.001, "total": 0.0015})
        assert _meta(spans["priced"], "cost_status") == "computed"
        assert "langfuse.observation.cost_details" not in spans["no-usage"].attributes
        assert _meta(spans["no-usage"], "cost_unavailable_reason") == "usage_missing"
        assert _meta(spans["no-usage"], "usage_status") == "unavailable"
    finally:
        tel.shutdown()


# ── trace propagation ────────────────────────────────────────────────


def test_traceparent_round_trip_across_services() -> None:
    gateway = Telemetry.for_testing("gateway")
    agent = Telemetry.for_testing("agent")
    try:
        with gateway.workflow_span("ingest") as root:
            headers = inject_trace_headers({"accept": "application/json"})
        assert set(headers) == {"accept", "traceparent"}  # no baggage, no user/session
        parent = extract_trace_context(headers)
        assert parent is not None
        assert (parent.trace_id, parent.span_id) == (root.trace_id, root.observation_id)

        with agent.workflow_span("extract", parent=parent) as child:
            assert child.trace_id == root.trace_id
        (span,) = _exported(agent)
        assert f"{span.parent.span_id:016x}" == root.observation_id
        assert span.parent.is_remote
    finally:
        gateway.shutdown()
        agent.shutdown()


def test_unsampled_incoming_context_cannot_switch_tracing_off(tel: Telemetry) -> None:
    header = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-00"
    with tel.workflow_span("extract", parent=extract_trace_context({"traceparent": header})):
        pass
    (span,) = _exported(tel)
    assert f"{span.context.trace_id:032x}" == "ab" * 16


VALID = "00-" + "4bf92f3577b34da6a3ce929d0e0e4736" + "-" + "00f067aa0ba902b7" + "-01"


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "garbage",
        VALID.upper(),
        "ff" + VALID[2:],
        "01" + VALID[2:],
        VALID + "-extra",
        VALID + "," + VALID,
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
        "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-zz",
        VALID + " " * 20,
    ],
)
def test_malformed_traceparent_is_rejected(value) -> None:
    assert parse_traceparent(value) is None


def test_traceparent_extraction_rules() -> None:
    assert parse_traceparent(f" {VALID}\t") is not None  # optional whitespace only
    assert extract_trace_context({"TraceParent": VALID}).trace_id == VALID[3:35]
    assert extract_trace_context({"traceparent": VALID, "TRACEPARENT": VALID}) is None
    assert extract_trace_context({}) is None
    assert extract_trace_context(None) is None


def test_httpx_hooks_inject_current_context(tel: Telemetry) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("traceparent"))
        return httpx.Response(200)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), event_hooks={"request": [httpx_request_hook]}
    )
    client.get("http://gateway.internal/x")
    with tel.workflow_span("resolve") as span:
        client.get("http://gateway.internal/x")
    assert seen[0] is None
    assert parse_traceparent(seen[1]).span_id == span.observation_id


async def test_async_httpx_hook(tel: Telemetry) -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("traceparent"))
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [async_httpx_request_hook]},
    ) as client:
        with tel.workflow_span("semantic_validation") as span:
            await client.post("http://validator.internal/validate")
    assert parse_traceparent(seen[0]).trace_id == span.trace_id


# ── identifiers, links, read-back ────────────────────────────────────


def test_pseudonyms_are_stable_and_not_the_raw_id() -> None:
    a = pseudonymize("user-matthew")
    assert a == pseudonymize("user-matthew")
    assert a != pseudonymize("user-ana")
    assert a != pseudonymize("user-matthew", key="service-secret")
    assert a.startswith("u_") and len(a) == 26 and "matthew" not in a


def test_trace_url_and_read_back(tel: Telemetry, monkeypatch) -> None:
    trace_id = "ab" * 16
    assert (
        tel.trace_url(trace_id) == f"http://langfuse.invalid/project/test-project/traces/{trace_id}"
    )
    assert tel.trace_url("not-a-trace") is None

    class _Obs:
        def model_dump(self, **_):
            return {"id": "1", "traceId": trace_id, "name": "extract"}

    class _Resp:
        data = [_Obs()]

    calls: dict = {}

    def get_many(**kwargs):
        calls.update(kwargs)
        return _Resp()

    monkeypatch.setattr(tel._backend.client.api.observations, "get_many", get_many)
    result = tel.read_back(trace_id)
    assert result.ok and result.observations[0]["name"] == "extract"
    assert calls["trace_id"] == trace_id
    assert calls["from_start_time"] < calls["to_start_time"]

    class _Gone(Exception):
        status_code = 410

    def gone(**_):
        raise _Gone()

    monkeypatch.setattr(tel._backend.client.api.observations, "get_many", gone)
    assert tel.read_back(trace_id).error == "read-back failed: _Gone (HTTP 410)"
    assert tel.read_back("bad").ok is False


# ── redaction and allowlist units ────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "DBS Savings ending 4521",
        "card ****4242",
        "2026-10-10",
        "2026-10-10 12:30:00",
        "2026-10-10T12:30:00.000Z",
        "amount 1234567.89",
        "123e4567-e89b-12d3-a456-426614174000",
        "trace 4bf92f3577b34da6a3ce929d0e0e4736",
        "sha " + "0123456789abcdef" * 4,
        "max_tokens=2048 prompt_tokens: 42",
        "acct-matthew-savings",
    ],
)
def test_redact_text_keeps_safe_values(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text, secret",
    [
        ("call +65 9123 4567", "9123 4567"),
        ("card 4242 4242 4242 4242.", "4242 4242 4242 4242"),
        ("acct 0123456789012", "0123456789012"),
        ("NRIC S1234567D", "S1234567D"),
        ("mail me at a.b@example.com", "a.b@example.com"),
        ("OTP: 482913", "482913"),
        ('{"password": "hunter2"}', "hunter2"),
        ("x-api-key=abcd1234efgh", "abcd1234efgh"),
        ("jwt eyJhbGciOiJI.eyJzdWIiOiIx.c2ln", "eyJhbGciOiJI"),
    ],
)
def test_redact_text_removes_secrets(text: str, secret: str) -> None:
    out = redact_text(text)
    assert secret not in out
    assert redact_text(out) == out  # idempotent


def test_sanitize_attributes_kinds() -> None:
    clean, dropped = sanitize_attributes(
        {
            "bundle_hash": "ab" * 32,
            "bundle_version": 3,
            "audit_seq": 12345678,  # 8 digits: looks like an account number
            "amount": Decimal("50.00"),
            "currency": "sgd",
            "account_masked": "1234567890",
            "step_up_required": "yes",
            "linked_trace_id": "ab" * 16,
            "service": "spoofed",  # reserved for the telemetry layer
            "unknown": "x",
        }
    )
    assert clean == {
        "bundle_hash": "ab" * 32,
        "bundle_version": 3,
        "amount": "50.00",
        "linked_trace_id": "ab" * 16,
    }
    assert dropped == 6


# ── boundary ─────────────────────────────────────────────────────────


def test_telemetry_package_has_no_service_imports() -> None:
    root = Path(__file__).resolve().parent.parent / "shared" / "telemetry"
    forbidden = {"gateway", "ledger", "agent", "validator", "os", "sys", "logging", "asyncio"}
    files = sorted(root.glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        assert not names & forbidden, f"{path.name} imports {names & forbidden}"
