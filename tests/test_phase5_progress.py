"""Actual stages, owned recovery, revision binding and draft-only cancellation."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from agent.workflow import _Nodes
from gateway.asr import AsrResult, _set_transcriber
from tests.agent_support import FakeLLM
from tests.orch_support import JOHN_TEXT, MUM, MUM_TEXT, Orch, o_fixture  # noqa: F401
from tests.t05_support import H, say
from tests.t10_support import v_fixture  # noqa: F401
from tests.test_t12_voice import make_wav
from tests.validator_fakes import RaisingLLM, ScriptedLLM


class HeldExtractor(FakeLLM):
    def __init__(self):
        super().__init__(MUM)
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, system_prompt, user_content):
        self.started.set()
        assert self.release.wait(3)
        return super().complete(system_prompt, user_content)


class HeldValidator(ScriptedLLM):
    def __init__(self):
        super().__init__('{"verdict":"MATCH","issues":[]}')
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, *, timeout_seconds):
        self.started.set()
        assert self.release.wait(3)
        return super().complete(messages, timeout_seconds=timeout_seconds)


def read(o: Orch, conv: str, operation: str, revision: int = 1):
    return o.client.get(
        f"/api/conversations/{conv}/progress",
        params={"operation_id": operation, "revision": revision},
    )


def test_progress_reports_actual_entered_stages_and_recovers_completed_draft(o, monkeypatch):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    extractor, validator = HeldExtractor(), HeldValidator()
    o.llm = extractor
    o.v.use_llm(validator)
    resolving, release_resolving = threading.Event(), threading.Event()
    original = _Nodes.resolve

    def held_resolve(self, state):
        resolving.set()
        assert release_resolving.wait(3)
        return original(self, state)

    monkeypatch.setattr(_Nodes, "resolve", held_resolve)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(o.draft, conv, json={"operation_id": operation, "revision": 1})
        try:
            assert extractor.started.wait(2)
            view = read(o, conv, operation).json()
            assert view["stage"] == "extracting" and not view["done"]
            assert view["result"] is None and view["elapsed_seconds"] >= 0
            extractor.release.set()
            assert resolving.wait(2)
            assert read(o, conv, operation).json()["stage"] == "resolving"
            release_resolving.set()
            assert validator.started.wait(2)
            assert read(o, conv, operation).json()["stage"] == "validating"
        finally:
            extractor.release.set()
            release_resolving.set()
            validator.release.set()
        result = request.result(timeout=3)
    assert result.status_code == 200
    view = read(o, conv, operation).json()
    assert view["stage"] == "ready" and view["done"]
    assert view["result"] == result.json()
    assert read(o, conv, operation).json()["elapsed_seconds"] == view["elapsed_seconds"]


def test_progress_is_owner_and_revision_scoped_and_never_recovers_stale_confirmation(o):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    assert o.draft(conv, json={"operation_id": operation, "revision": 1}).status_code == 200
    assert read(o, conv, operation, 0).status_code == 409
    o.env.as_user("user-ana")
    assert read(o, conv, operation).status_code == 404
    o.env.as_user("user-matthew")
    say(o.env, conv, "Actually make it fifteen dollars")
    stale = read(o, conv, operation)
    assert stale.status_code == 409 and stale.json()["code"] == "CONVERSATION_CHANGED"
    assert "result" not in stale.json()
    assert o.draft(conv, json={"revision": 1}).json()["code"] == "STALE_CONVERSATION_REVISION"
    assert len(o.llm.calls) == 1


def test_changed_bank_binding_does_not_recover_ready_confirmation(o):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    o.draft(conv, json={"operation_id": operation})
    o.v.sql("UPDATE payees SET masked_suffix = '9999' WHERE payee_id = 'payee-mum'")
    view = read(o, conv, operation).json()
    assert view["stage"] == "failed" and view["result"] is None
    assert view["code"] == "VALIDATION_NOT_ELIGIBLE"


def test_progress_needs_input_and_configuration_failure_starts_no_extraction(o, monkeypatch):
    conv, operation = o.conversation(JOHN_TEXT), str(uuid.uuid4())
    from tests.orch_support import JOHN

    o.extractor(JOHN)
    assert o.draft(conv, json={"operation_id": operation}).json()["status"] == "clarify"
    assert read(o, conv, operation).json()["stage"] == "needs_input"
    monkeypatch.setattr("gateway.orchestration.drafting_problem", lambda: "SERVICE_TOKEN_UNSET")
    failed = str(uuid.uuid4())
    response = o.draft(conv, json={"operation_id": failed})
    assert response.status_code == 503
    progress = read(o, conv, failed).json()
    assert progress["stage"] == "failed" and progress["code"] == "SERVICE_TOKEN_UNSET"
    assert len(o.llm.calls) == 1


def test_explicit_validation_retry_reuses_frozen_work_and_cannot_extract(o):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    assert o.draft(conv, json={"mode": "validation"}).json()["code"] == "REDRAFT_REQUIRED"
    assert o.llm.calls == []
    o.v.use_llm(RaisingLLM(TimeoutError()))
    assert o.draft(conv, json={"operation_id": operation}).status_code != 200
    assert read(o, conv, operation).json()["retry_validation"]
    o.v.clock.advance(seconds=3)
    o.v.use_llm(ScriptedLLM.verdict("MATCH"))
    retry = str(uuid.uuid4())
    assert o.draft(conv, json={"operation_id": retry, "mode": "validation"}).status_code == 200
    assert len(o.llm.calls) == 1
    assert read(o, conv, retry).json()["stage"] == "ready"


def test_cancel_stops_draft_and_late_callbacks_without_cancelling_committed_payment(o):
    paid_conv = o.conversation(MUM_TEXT)
    draft = o.drafted(paid_conv)
    authz = o.v.issue(draft["bundle_id"]).json()
    receipt = o.v.approve(authz)
    assert receipt.json()["status"] == "COMMITTED"
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    held = HeldExtractor()
    o.llm = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(o.draft, conv, json={"operation_id": operation})
        try:
            assert held.started.wait(2)
            o.env.as_user("user-ana")
            path = f"/api/conversations/{conv}/draft/cancel"
            body = {"operation_id": operation, "revision": 1}
            assert o.client.post(path, json=body, headers=H).status_code == 404
            o.env.as_user("user-matthew")
            assert o.client.post(path, json=body, headers=H).json() == {"status": "draft_stopped"}
            view = read(o, conv, operation).json()
            assert view["done"] and view["code"] == "DRAFT_CANCELLED"
        finally:
            held.release.set()
        assert request.result(timeout=3).status_code != 200
    assert o.v.q("SELECT bundle_id FROM frozen_drafts WHERE conversation_id = ?", (conv,)) == []
    recovered = o.client.get(f"/api/authorizations/{authz['authorization_id']}/receipt")
    assert recovered.json()["receipt_id"] == receipt.json()["receipt_id"]


def test_voice_progress_changes_to_transcribing_only_when_asr_is_entered(o):
    conv, operation = o.conversation(), str(uuid.uuid4())
    started, release = threading.Event(), threading.Event()

    class Transcriber:
        ready = True

        def transcribe(self, audio, *, hotwords):
            started.set()
            assert release.wait(3)
            return AsrResult(text=MUM_TEXT, request_id="fake", latency_ms=1, audio_duration_ms=1000)

    _set_transcriber(Transcriber())
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            o.client.post,
            f"/api/conversations/{conv}/voice",
            params={"operation_id": operation},
            headers=H,
            files={"audio": ("clip.wav", make_wav(), "audio/wav")},
            data={"consent": "true"},
        )
        try:
            assert started.wait(2)
            assert read(o, conv, operation, 0).json()["stage"] == "transcribing"
        finally:
            release.set()
            _set_transcriber(None)
        result = request.result(timeout=3)
    assert result.status_code == 200, result.text
    recovered = read(o, conv, operation, 0).json()
    assert recovered["stage"] == "captured" and recovered["done"]
    assert recovered["result"]["turn"]["conversation_revision"] == 1
    say(o.env, conv, "Actually make it fifteen dollars")
    assert read(o, conv, operation, 0).status_code == 409


def test_captured_until_readiness_and_deadline_failure_has_no_late_stage(o, monkeypatch):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    started, release = threading.Event(), threading.Event()

    def readiness():
        started.set()
        assert release.wait(3)
        return "SERVICE_TOKEN_UNSET"

    monkeypatch.setattr("gateway.orchestration.drafting_problem", readiness)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(o.draft, conv, json={"operation_id": operation})
        try:
            assert started.wait(2)
            view = read(o, conv, operation).json()
            assert view["stage"] == "captured" and view["result"] is None
        finally:
            release.set()
        assert request.result(timeout=3).status_code == 503
    assert read(o, conv, operation).json()["stage"] == "failed"
    assert o.llm.calls == []


def test_agent_progress_cannot_forge_terminal_or_change_revision(o):
    conv, operation = o.conversation(MUM_TEXT), str(uuid.uuid4())
    held = HeldExtractor()
    o.llm = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(o.draft, conv, json={"operation_id": operation})
        try:
            assert held.started.wait(2)
            headers = {"Authorization": o.agent_requests[-1].headers["authorization"]}
            path = f"/internal/conversations/{conv}/progress"
            assert (
                o.client.post(
                    path, headers=headers, json={"revision": 1, "stage": "ready"}
                ).status_code
                == 422
            )
            assert (
                o.client.post(
                    path, headers=headers, json={"revision": 2, "stage": "resolving"}
                ).status_code
                == 409
            )
            assert read(o, conv, operation).json()["stage"] == "extracting"
        finally:
            held.release.set()
        assert request.result(timeout=3).status_code == 200
    assert (
        o.client.post(
            path, headers=headers, json={"revision": 1, "stage": "extracting"}
        ).status_code
        == 409
    )
    assert read(o, conv, operation).json()["stage"] == "ready"


def test_progress_store_is_bounded_and_terminal_records_expire(monkeypatch):
    import gateway.progress as module

    monkeypatch.setattr(module, "_records", {})
    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    records = [module.Progress("fake-db", "owner", "conv", 1) for _ in range(3)]
    for record in records:
        module.register(record)
    assert len(module._records) == 2
    assert module.get("fake-db", "owner", "conv", records[0].operation_id) is None
    assert module.get("fake-db", "other", "conv", records[1].operation_id) is None
    records[1].finish(code="DRAFT_CANCELLED")
    records[1].update("validating")
    assert records[1].view()["stage"] == "failed"
    records[1].ended -= module.RETENTION_SECONDS + 1
    assert module.get("fake-db", "owner", "conv", records[1].operation_id) is None
