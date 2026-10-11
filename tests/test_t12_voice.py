"""T-12 voice: WAV checks, stored evidence, gateway-side ASR and the single final turn.

Real routes, a temp SQLite DB and a temp audio directory. The only substitutions are
test-only hooks unreachable over HTTP: the user-session dependency override, a fake
transcriber (``gateway.asr._set_transcriber``) and an in-memory telemetry exporter
(``gateway.asr_routes._set_voice_telemetry``). The TRTC adapter itself is exercised
with a fake SDK recognizer so no network call is made.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import threading
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from trtc_asr.errors import ASRError

from gateway.app import app
from gateway.asr import (
    AsrError,
    AsrResult,
    AudioRejected,
    DuplicateSegment,
    TrtcTranscriber,
    _set_transcriber,
    append_voice_turn,
    classify_asr_exception,
    validate_wav,
)
from gateway.asr_routes import _set_voice_telemetry
from gateway.auth.session import UserPrincipal, require_user_session
from gateway.bank import BankStore
from gateway.bank_routes import _set_bank_store
from gateway.conv_routes import _set_conversation_store
from gateway.conversations import ConversationStore
from shared.settings import load_gateway_settings
from shared.telemetry import Telemetry

ORIGIN = load_gateway_settings().rp_origin
H = {"Origin": ORIGIN}
TRANSCRIPT = "Send fifty dollars to Mum from my savings"
SECRET_CANARY = "SDKSECRET-CANARY-7f3a9c1e"


# ── WAV builder ──────────────────────────────────────────────────────


def make_wav(
    seconds: float = 1.0,
    *,
    rate: int = 16_000,
    channels: int = 1,
    bits: int = 16,
    fmt_tag: int = 1,
    frames: int | None = None,
    pre_chunks: bytes = b"",
    extensible_guid: bytes | None = None,
    declared_data: int | None = None,
) -> bytes:
    """A canonical RIFF/WAVE file with a 440 Hz tone (deterministic bytes)."""
    n = int(seconds * rate) if frames is None else frames
    width = bits // 8
    block = channels * width
    second = bytearray()  # one second of tone, tiled to the requested length
    for i in range(min(n, rate)):
        value = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        for _ in range(channels):
            if width == 1:
                second += bytes([((value >> 8) + 128) & 0xFF])
            else:
                second += value.to_bytes(2, "little", signed=True) + b"\x00" * (width - 2)
    repeats = -(-n // rate) if n else 0
    samples = (bytes(second) * repeats)[: n * block]
    if extensible_guid is not None:
        fmt_body = (
            (0xFFFE).to_bytes(2, "little")
            + channels.to_bytes(2, "little")
            + rate.to_bytes(4, "little")
            + (rate * block).to_bytes(4, "little")
            + block.to_bytes(2, "little")
            + bits.to_bytes(2, "little")
            + (22).to_bytes(2, "little")
            + bits.to_bytes(2, "little")
            + (4).to_bytes(4, "little")
            + extensible_guid
        )
    else:
        fmt_body = (
            fmt_tag.to_bytes(2, "little")
            + channels.to_bytes(2, "little")
            + rate.to_bytes(4, "little")
            + (rate * block).to_bytes(4, "little")
            + block.to_bytes(2, "little")
            + bits.to_bytes(2, "little")
        )
    fmt = b"fmt " + len(fmt_body).to_bytes(4, "little") + fmt_body
    size = len(samples) if declared_data is None else declared_data
    data = b"data" + size.to_bytes(4, "little") + bytes(samples)
    body = b"WAVE" + pre_chunks + fmt + data
    return b"RIFF" + len(body).to_bytes(4, "little") + body


PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")
FLOAT_GUID = bytes.fromhex("0300000000001000800000aa00389b71")


# ── Fakes and fixture ────────────────────────────────────────────────


@dataclass
class FakeTranscriber:
    """Records the exact bytes it receives; returns ``result`` or raises ``error``."""

    result: AsrResult = field(
        default_factory=lambda: AsrResult(TRANSCRIPT, "req-fake-0001", 1000, 12)
    )
    error: BaseException | None = None
    hook: Any = None
    calls: list[bytes] = field(default_factory=list)
    hotwords: list[list[str]] = field(default_factory=list)
    ready: bool = True

    def transcribe(self, wav_bytes: bytes, hotwords=()) -> AsrResult:
        self.calls.append(wav_bytes)
        self.hotwords.append(list(hotwords))
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class V:
    client: TestClient
    store: ConversationStore
    db_path: str
    audio_dir: Path
    fake: FakeTranscriber
    tel: Telemetry

    def as_user(self, user_id: str) -> None:
        app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id=user_id)

    def new_conversation(self) -> str:
        resp = self.client.post("/api/conversations", json={})
        assert resp.status_code == 200, resp.text
        return resp.json()["conversation_id"]

    def upload(
        self,
        conv_id: str,
        wav: bytes | None,
        *,
        content_type: str = "audio/wav",
        consent: str | None = "true",
        headers: dict[str, str] | None = H,
    ) -> Any:
        files = {"audio": ("clip.wav", wav, content_type)} if wav is not None else None
        data = {"consent": consent} if consent is not None else {}
        if files is None:
            data["placeholder"] = "x"  # keep the body multipart without an audio part
        return self.client.post(
            f"/api/conversations/{conv_id}/voice",
            files=files,
            data=data,
            headers=headers or {},
        )

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def audio_rows(self, conv_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM audio_evidence WHERE conversation_id = ?", (conv_id,))

    def turns(self, conv_id: str, role: str | None = None) -> list[sqlite3.Row]:
        if role is None:
            return self.rows(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? ORDER BY seq",
                (conv_id,),
            )
        return self.rows(
            "SELECT * FROM conversation_turns WHERE conversation_id = ? AND role = ? ORDER BY seq",
            (conv_id, role),
        )

    def revision(self, conv_id: str) -> int:
        return int(
            self.rows("SELECT revision FROM conversations WHERE conversation_id = ?", (conv_id,))[
                0
            ]["revision"]
        )

    def audit(self, conv_id: str) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM audit_events WHERE conversation_id = ? ORDER BY seq", (conv_id,)
        )

    def audit_steps(self, conv_id: str) -> list[str]:
        return [r["step"] for r in self.audit(conv_id)]

    def audio_files(self) -> list[Path]:
        return sorted(self.audio_dir.glob("*")) if self.audio_dir.exists() else []

    def spans(self) -> list[Any]:
        self.tel.flush()
        return list(self.tel.exporter.get_finished_spans())


@pytest.fixture()
def v(tmp_path: Path):
    db_path = str(tmp_path / "dcta.db")
    bank = BankStore(db_path)
    bank.init_and_seed()
    _set_bank_store(bank)
    audio_dir = tmp_path / "audio"
    store = ConversationStore(db_path, audio_dir=str(audio_dir) + "/")
    _set_conversation_store(store)
    fake = FakeTranscriber()
    _set_transcriber(fake)
    tel = Telemetry.for_testing("gateway")
    _set_voice_telemetry(tel)
    app.dependency_overrides[require_user_session] = lambda: UserPrincipal(user_id="user-matthew")
    env = V(TestClient(app), store, db_path, audio_dir, fake, tel)
    yield env
    app.dependency_overrides.clear()
    _set_transcriber(None)
    _set_voice_telemetry(None)
    tel.shutdown()
    _set_conversation_store(None)
    _set_bank_store(None)


def _meta(span: Any, key: str) -> Any:
    return span.attributes.get(f"langfuse.observation.metadata.{key}")


def _dump(spans: list[Any]) -> str:
    return json.dumps(
        [{"name": s.name, **dict(s.attributes or {}), "st": s.status.description} for s in spans],
        default=str,
        ensure_ascii=False,
    )


def _ingest(spans: list[Any]) -> Any:
    found = [s for s in spans if s.name == "ingest"]
    assert len(found) == 1, [s.name for s in spans]
    return found[0]


# ── Happy path ───────────────────────────────────────────────────────


def test_valid_wav_is_stored_transcribed_and_appended_once(v: V) -> None:
    conv = v.new_conversation()
    wav = make_wav(1.5)
    resp = v.upload(conv, wav)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    digest = hashlib.sha256(wav).hexdigest()
    assert body["digest"] == digest
    assert body["transcript"] == TRANSCRIPT
    assert body["duration_ms"] == 1500
    assert body["consent"] is True
    assert body["asr"] == {
        "request_id": "req-fake-0001",
        "latency_ms": 12,
        "audio_duration_ms": 1000,
        "engine": "bigmodel",
        "language": "en",
    }
    turn = body["turn"]
    assert turn["role"] == "user_asr_final"
    assert turn["text"] == TRANSCRIPT
    assert turn["conversation_revision"] == 1

    # Stored bytes == uploaded bytes == bytes the transcriber received.
    [row] = v.audio_rows(conv)
    stored = Path(row["file_path"]).read_bytes()
    assert stored == wav
    assert v.fake.calls == [wav]
    assert row["sha256_digest"] == digest == hashlib.sha256(v.fake.calls[0]).hexdigest()
    # The user's own saved names are sent as ASR hotwords (only theirs, never Ana's).
    sent = {w.casefold() for w in v.fake.hotwords[0]}
    assert {"ah ma", "mum", "john doe", "sp group"} <= sent
    assert "dad" not in sent  # Ana's alias
    assert row["consent"] == 1 and row["retention_until"] == body["retention_until"]
    assert row["content_type"] == "audio/wav" and row["size_bytes"] == len(wav)
    # The clip is linked to exactly its one final turn; the revision moved once.
    assert row["turn_id"] == turn["turn_id"]
    assert [t["turn_id"] for t in v.turns(conv)] == [turn["turn_id"]]
    assert v.revision(conv) == 1

    # The conversation route shows the gateway-produced turn.
    conv_view = v.client.get(f"/api/conversations/{conv}").json()
    assert [(t["role"], t["text"]) for t in conv_view["turns"]] == [("user_asr_final", TRANSCRIPT)]


def test_every_step_is_audited_and_chain_verifies(v: V) -> None:
    conv = v.new_conversation()
    wav = make_wav(0.5)
    body = v.upload(conv, wav).json()
    steps = v.audit_steps(conv)
    assert steps == [
        "gateway.conversation.create",
        "gateway.audio.upload",
        "gateway.voice.asr_started",
        "gateway.conversation.revision",
        "gateway.voice.transcribed",
    ]
    rows = {r["step"]: r for r in v.audit(conv)}
    started = json.loads(rows["gateway.voice.asr_started"]["payload_json"])
    assert started["audio_id"] == body["audio_id"]
    assert started["sha256_digest"] == started["sent_sha256"] == body["digest"]
    done = json.loads(rows["gateway.voice.transcribed"]["payload_json"])
    assert done["turn_id"] == body["turn"]["turn_id"]
    assert done["asr_request_id"] == "req-fake-0001"
    assert done["transcript_chars"] == len(TRANSCRIPT)
    # Trace correlation: audit rows carry the ingest span's trace ID.
    span = _ingest(v.spans())
    assert rows["gateway.voice.transcribed"]["trace_id"] == f"{span.context.trace_id:032x}"
    assert body["trace_id"] == f"{span.context.trace_id:032x}"
    verify = v.client.get(f"/api/conversations/{conv}/audit/verify").json()
    assert verify["ok"] is True


def test_user_can_correct_the_transcript(v: V) -> None:
    conv = v.new_conversation()
    v.fake.result = AsrResult("Send fifteen dollars to Mum", "req-2", 900, 10)
    turn = v.upload(conv, make_wav(0.5)).json()["turn"]
    resp = v.client.post(
        f"/api/conversations/{conv}/transcript-edit",
        json={"edits_turn_id": turn["turn_id"], "text": "Send fifty dollars to Mum"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["conversation_revision"] == 2
    roles = [(t["role"], t["text"]) for t in v.turns(conv)]
    assert roles == [
        ("user_asr_final", "Send fifteen dollars to Mum"),
        ("user_transcript_edit", "Send fifty dollars to Mum"),
    ]


# ── Upload validation ────────────────────────────────────────────────

_REJECTS = [
    ("not_riff", b"ID3\x04" + b"\x00" * 200, "audio/wav", 415, "AUDIO_NOT_WAV"),
    ("webm_renamed", b"\x1aE\xdf\xa3" + b"\x00" * 200, "audio/wav", 415, "AUDIO_NOT_WAV"),
    ("wrong_content_type", make_wav(0.2), "audio/mpeg", 415, "AUDIO_NOT_WAV"),
    ("octet_stream", make_wav(0.2), "application/octet-stream", 415, "AUDIO_NOT_WAV"),
    ("stereo", make_wav(0.2, channels=2), "audio/wav", 415, "AUDIO_NOT_MONO"),
    ("44k1", make_wav(0.2, rate=44_100), "audio/wav", 415, "AUDIO_NOT_16KHZ"),
    ("8bit", make_wav(0.2, bits=8), "audio/wav", 415, "AUDIO_NOT_16BIT"),
    ("float32", make_wav(0.1, bits=32, fmt_tag=3), "audio/wav", 415, "AUDIO_NOT_PCM"),
    (
        "extensible_float",
        make_wav(0.1, bits=32, extensible_guid=FLOAT_GUID),
        "audio/wav",
        415,
        "AUDIO_NOT_PCM",
    ),
    ("truncated", make_wav(0.2, declared_data=99_999), "audio/wav", 415, "AUDIO_NOT_WAV"),
    ("no_samples", make_wav(frames=0), "audio/wav", 400, "AUDIO_EMPTY"),
    ("empty_upload", b"", "audio/wav", 400, "AUDIO_EMPTY"),
    ("too_long", make_wav(61.0), "audio/wav", 413, "AUDIO_TOO_LONG"),
    ("oversized", make_wav(100.0), "audio/wav", 413, "AUDIO_TOO_LARGE"),
]


@pytest.mark.parametrize(
    ("wav", "ctype", "http", "code"),
    [r[1:] for r in _REJECTS],
    ids=[r[0] for r in _REJECTS],
)
def test_bad_uploads_are_rejected_before_storage_or_asr(
    v: V, wav: bytes, ctype: str, http: int, code: str
) -> None:
    conv = v.new_conversation()
    resp = v.upload(conv, wav, content_type=ctype)
    assert resp.status_code == http, resp.text
    assert resp.json()["detail"]["code"] == code
    assert v.audio_rows(conv) == [] and v.audio_files() == []
    assert v.fake.calls == []
    assert v.revision(conv) == 0 and v.turns(conv) == []


def test_oversize_check_does_not_trust_the_wav_header() -> None:
    assert len(make_wav(100.0)) > 3 * 1024 * 1024
    with pytest.raises(AudioRejected) as err:
        validate_wav(make_wav(100.0))
    assert err.value.code == "AUDIO_TOO_LARGE"


def test_wav_parser_accepts_padding_chunks_extensible_pcm_and_the_60s_boundary() -> None:
    junk = b"JUNK" + (28).to_bytes(4, "little") + b"\x00" * 28
    fllr = b"FLLR" + (5).to_bytes(4, "little") + b"\x00" * 5 + b"\x00"  # odd size + pad byte
    info = validate_wav(make_wav(0.25, pre_chunks=junk + fllr))
    assert (info.sample_rate, info.channels, info.bits_per_sample) == (16_000, 1, 16)
    assert info.duration_ms == 250
    assert validate_wav(make_wav(0.1, extensible_guid=PCM_GUID)).duration_ms == 100
    assert validate_wav(make_wav(frames=16_000 * 60)).duration_ms == 60_000
    with pytest.raises(AudioRejected) as err:
        validate_wav(make_wav(frames=16_000 * 60 + 1))
    assert err.value.code == "AUDIO_TOO_LONG"


def test_consent_is_required_to_store_audio(v: V) -> None:
    conv = v.new_conversation()
    for consent in (None, "false", "yes", ""):
        resp = v.upload(conv, make_wav(0.3), consent=consent)
        assert resp.status_code == 400, (consent, resp.text)
        assert resp.json()["detail"]["code"] == "AUDIO_CONSENT_REQUIRED"
    assert v.audio_rows(conv) == [] and v.audio_files() == [] and v.fake.calls == []


def test_missing_audio_field(v: V) -> None:
    conv = v.new_conversation()
    resp = v.upload(conv, None)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "AUDIO_MISSING"


# ── Origin, ownership and the removed client ASR route ──────────────


@pytest.mark.parametrize("headers", [None, {"Origin": "http://evil.example"}])
def test_origin_is_required(v: V, headers: dict[str, str] | None) -> None:
    conv = v.new_conversation()
    resp = v.upload(conv, make_wav(0.3), headers=headers)
    assert resp.status_code == 403
    assert v.audio_rows(conv) == [] and v.fake.calls == []


def test_other_user_cannot_upload_or_read_matthews_audio(v: V) -> None:
    conv = v.new_conversation()
    wav = make_wav(0.4)
    audio_id = v.upload(conv, wav).json()["audio_id"]
    v.fake.calls.clear()

    v.as_user("user-ana")
    resp = v.upload(conv, make_wav(0.5))
    assert resp.status_code == 404
    assert v.client.get(f"/api/conversations/{conv}/voice").status_code == 404
    assert v.client.get(f"/api/conversations/{conv}/voice/{audio_id}").status_code == 404
    # Ana's own conversation cannot be used to reach Matthew's clip either.
    ana_conv = v.new_conversation()
    assert v.client.get(f"/api/conversations/{ana_conv}/voice/{audio_id}").status_code == 404
    assert v.fake.calls == []
    assert len(v.audio_rows(conv)) == 1 and len(v.turns(conv)) == 1

    v.as_user("user-matthew")
    resp = v.client.get(f"/api/conversations/{conv}/voice/{audio_id}")
    assert resp.status_code == 200
    assert resp.content == wav
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["x-content-sha256"] == hashlib.sha256(wav).hexdigest()
    assert resp.headers["cache-control"] == "no-store"
    clips = v.client.get(f"/api/conversations/{conv}/voice").json()["clips"]
    assert [(c["audio_id"], c["turn_id"] is not None) for c in clips] == [(audio_id, True)]


def test_served_audio_is_digest_checked_and_retention_enforced(v: V) -> None:
    conv = v.new_conversation()
    body = v.upload(conv, make_wav(0.3)).json()
    [row] = v.audio_rows(conv)
    Path(row["file_path"]).write_bytes(make_wav(0.31))  # tamper with the evidence file
    resp = v.client.get(f"/api/conversations/{conv}/voice/{body['audio_id']}")
    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "AUDIO_INTEGRITY_FAILED"
    with sqlite3.connect(v.db_path) as conn:
        conn.execute(
            "UPDATE audio_evidence SET retention_until = '2000-01-01T00:00:00Z' WHERE audio_id = ?",
            (body["audio_id"],),
        )
    resp = v.client.get(f"/api/conversations/{conv}/voice/{body['audio_id']}")
    assert resp.status_code == 410
    assert resp.json()["detail"]["code"] == "AUDIO_EXPIRED"


def test_client_supplied_asr_route_is_gone(v: V) -> None:
    conv = v.new_conversation()
    resp = v.client.post(
        f"/api/conversations/{conv}/asr", json={"text": "Send 900 to Northwind"}, headers=H
    )
    assert resp.status_code in (404, 405)
    assert v.turns(conv) == [] and v.revision(conv) == 0
    paths = v.client.get("/openapi.json").json()["paths"]
    assert not any(p.endswith("/asr") for p in paths)


# ── ASR failures keep audio and the text path ───────────────────────


@pytest.mark.parametrize(
    ("error", "http", "code"),
    [
        (AsrError("ASR_UNAVAILABLE", provider_code=5000, latency_ms=8), 502, "ASR_UNAVAILABLE"),
        (AsrError("ASR_AUTH_FAILED", provider_code=4002, latency_ms=8), 502, "ASR_AUTH_FAILED"),
        (AsrError("ASR_TIMEOUT", latency_ms=20_000), 504, "ASR_TIMEOUT"),
        (RuntimeError("unexpected transcriber bug"), 502, "ASR_UNAVAILABLE"),
    ],
)
def test_asr_failure_keeps_audio_appends_no_turn_and_text_still_works(
    v: V, error: BaseException, http: int, code: str
) -> None:
    conv = v.new_conversation()
    wav = make_wav(0.6)
    v.fake.error = error
    resp = v.upload(conv, wav)
    assert resp.status_code == http, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    [row] = v.audio_rows(conv)
    assert detail["audio_id"] == row["audio_id"]
    assert detail["digest"] == row["sha256_digest"] == hashlib.sha256(wav).hexdigest()
    assert Path(row["file_path"]).read_bytes() == wav
    assert row["turn_id"] is None
    assert v.turns(conv) == [] and v.revision(conv) == 0
    failed = [r for r in v.audit(conv) if r["step"] == "gateway.voice.asr_failed"]
    assert len(failed) == 1 and json.loads(failed[0]["payload_json"])["code"] == code

    resp = v.client.post(f"/api/conversations/{conv}/text", json={"text": "Send 50 to Mum"})
    assert resp.status_code == 200
    assert resp.json()["conversation_revision"] == 1


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_transcript_is_no_speech_and_appends_nothing(v: V, text: str) -> None:
    conv = v.new_conversation()
    v.fake.result = AsrResult(text, "req-silent", 700, 9)
    resp = v.upload(conv, make_wav(0.7))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "ASR_NO_SPEECH" and detail["audio_id"]
    assert v.turns(conv) == [] and v.revision(conv) == 0
    assert len(v.audio_rows(conv)) == 1
    failed = [r for r in v.audit(conv) if r["step"] == "gateway.voice.asr_failed"]
    assert json.loads(failed[0]["payload_json"])["asr_request_id"] == "req-silent"


def test_over_long_transcript_asks_for_restatement(v: V) -> None:
    conv = v.new_conversation()
    v.fake.result = AsrResult("a" * 2001, "req-long", 59_000, 30)
    resp = v.upload(conv, make_wav(0.3))
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "CONTEXT_LIMIT_RESTATE"
    assert v.turns(conv) == [] and len(v.audio_rows(conv)) == 1
    assert "gateway.voice.turn_rejected" in v.audit_steps(conv)


def test_unconfigured_trtc_stores_nothing(v: V) -> None:
    _set_transcriber(TrtcTranscriber(None, None, "asr-intl.cloud-rtc.com"))
    conv = v.new_conversation()
    resp = v.upload(conv, make_wav(0.3))
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "ASR_UNAVAILABLE"
    assert v.audio_rows(conv) == [] and v.audio_files() == []


# ── Duplicate final segments ────────────────────────────────────────


def test_same_clip_cannot_create_a_second_final_segment(v: V) -> None:
    conv = v.new_conversation()
    wav = make_wav(0.8)
    first = v.upload(conv, wav).json()
    again = v.upload(conv, wav)
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "AUDIO_DUPLICATE"
    assert detail["turn_id"] == first["turn"]["turn_id"]
    assert detail["audio_id"] == first["audio_id"]
    assert len(v.fake.calls) == 1
    assert len(v.turns(conv, "user_asr_final")) == 1 and len(v.audio_rows(conv)) == 1

    # A different clip is a new segment.
    assert v.upload(conv, make_wav(0.9)).status_code == 200
    assert len(v.turns(conv, "user_asr_final")) == 2


def test_one_audio_id_links_to_at_most_one_turn(v: V) -> None:
    conv = v.new_conversation()
    audio_id = v.upload(conv, make_wav(0.5)).json()["audio_id"]
    with pytest.raises(DuplicateSegment):
        append_voice_turn(
            v.store, "user-matthew", conv, audio_id, "replayed text", audit_payload={}
        )
    assert len(v.turns(conv)) == 1


def test_concurrent_identical_uploads_create_one_turn(v: V) -> None:
    conv = v.new_conversation()
    wav = make_wav(0.5)
    barrier = threading.Barrier(2, timeout=10)
    v.fake.hook = barrier.wait  # both requests are past the pre-check and inside ASR
    results: list[int] = []

    def go() -> None:
        results.append(v.upload(conv, wav).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == [200, 409]
    assert len(v.turns(conv, "user_asr_final")) == 1
    linked = [r for r in v.audio_rows(conv) if r["turn_id"] is not None]
    assert len(linked) == 1


# ── TRTC adapter: request shape, error mapping, no secrets ──────────


class FakeRecognizer:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[bytes, Any]] = []

    def recognize_data_with_options(self, data: bytes, req: Any) -> Any:
        self.calls.append((data, req))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _trtc(outcome: Any) -> tuple[TrtcTranscriber, FakeRecognizer, list[tuple]]:
    recognizer = FakeRecognizer(outcome)
    made: list[tuple] = []

    def factory(app_id: int, secret: str, host: str, timeout: float) -> FakeRecognizer:
        made.append((app_id, secret, host, timeout))
        return recognizer

    return (
        TrtcTranscriber(
            "1400000001", SECRET_CANARY, "asr-intl.cloud-rtc.com", recognizer_factory=factory
        ),
        recognizer,
        made,
    )


def test_trtc_adapter_sends_stored_bytes_with_contract_parameters(v: V) -> None:
    response = SimpleNamespace(request_id="5ed90438-c1d8", audio_duration=1719, result=TRANSCRIPT)
    trtc, recognizer, made = _trtc(response)
    _set_transcriber(trtc)
    conv = v.new_conversation()
    wav = make_wav(1.0)
    resp = v.upload(conv, wav)
    assert resp.status_code == 200, resp.text
    assert made == [(1400000001, SECRET_CANARY, "asr-intl.cloud-rtc.com", 20.0)]
    [(sent, req)] = recognizer.calls
    [row] = v.audio_rows(conv)
    assert sent == wav == Path(row["file_path"]).read_bytes()
    assert hashlib.sha256(sent).hexdigest() == row["sha256_digest"]
    assert (req.engine_model_type, req.voice_format, req.language) == ("bigmodel", "wav", "en")
    assert resp.json()["asr"]["request_id"] == "5ed90438-c1d8"
    assert resp.json()["asr"]["audio_duration_ms"] == 1719


def _timeout_asr_error() -> ASRError:
    try:
        try:
            raise urllib.error.URLError(TimeoutError("timed out"))
        except Exception as exc:  # noqa: BLE001
            raise ASRError(1002, f"http request failed: {exc}")  # noqa: B904 - as the SDK does
    except ASRError as err:
        return err


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ASRError(4002, "auth"), "ASR_AUTH_FAILED"),
        (ASRError(1005, "generate user sig failed"), "ASR_AUTH_FAILED"),
        (ASRError(1006, "timeout"), "ASR_TIMEOUT"),
        (_timeout_asr_error(), "ASR_TIMEOUT"),
        (ASRError(1002, "http request failed: connection refused"), "ASR_UNAVAILABLE"),
        (ASRError(4001, "invalid param"), "ASR_UNAVAILABLE"),
        (ASRError(5000, "server"), "ASR_UNAVAILABLE"),
        (RuntimeError("boom"), "ASR_UNAVAILABLE"),
    ],
)
def test_asr_error_classification(exc: BaseException, code: str) -> None:
    assert classify_asr_exception(exc)[0] == code
    trtc, _, _ = _trtc(exc)
    with pytest.raises(AsrError) as err:
        trtc.transcribe(make_wav(0.1))
    assert err.value.code == code
    assert SECRET_CANARY not in repr(trtc)


def test_no_audio_transcript_or_secret_in_audit_telemetry_or_errors(v: V) -> None:
    wav = make_wav(0.5)
    audio_b64 = base64.b64encode(wav).decode()
    pcm_b64 = base64.b64encode(wav[44:200]).decode()

    # Failure: the SDK error message echoes the secret and some audio.
    leak = ASRError(4002, f"bad usersig for {SECRET_CANARY} data={audio_b64[:120]}")
    trtc, _, _ = _trtc(leak)
    _set_transcriber(trtc)
    conv = v.new_conversation()
    failed = v.upload(conv, wav)
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "ASR_AUTH_FAILED"

    # Success: the transcript lives in the turn, not in audit payloads or telemetry.
    canary_text = "Pay Ah Ma one hundred dollars CANARYWORD"
    ok, _, _ = _trtc(SimpleNamespace(request_id="req-ok", audio_duration=500, result=canary_text))
    _set_transcriber(ok)
    conv2 = v.new_conversation()
    good = v.upload(conv2, make_wav(0.55))
    assert good.status_code == 200

    audit_blob = json.dumps([dict(r) for r in v.audit(conv) + v.audit(conv2)], default=str)
    telemetry_blob = _dump(v.spans())
    for blob in (audit_blob, telemetry_blob, failed.text):
        assert SECRET_CANARY not in blob
        assert audio_b64[:40] not in blob and pcm_b64[:40] not in blob
        assert "bad usersig" not in blob
    assert "CANARYWORD" not in audit_blob and "CANARYWORD" not in telemetry_blob
    assert "RIFF" not in telemetry_blob


def test_ingest_span_records_voice_facts(v: V) -> None:
    conv = v.new_conversation()
    v.upload(conv, make_wav(0.5))
    v.fake.error = AsrError("ASR_TIMEOUT", latency_ms=20_000)
    v.upload(conv, make_wav(0.6))
    spans = [s for s in v.spans() if s.name == "ingest"]
    assert len(spans) == 2
    ok, timeout = spans
    assert _meta(ok, "channel") == "voice"
    assert _meta(ok, "request_id") == "req-fake-0001"
    assert _meta(ok, "latency_ms") == 12
    assert _meta(ok, "outcome") == "ok"
    assert _meta(ok, "conversation_revision") == 1
    assert _meta(ok, "turn_id")
    output = json.loads(ok.attributes["langfuse.observation.output"])
    assert output["audio_duration_ms"] == 1000 and output["clip_duration_ms"] == 500
    assert output["transcript_chars"] == len(TRANSCRIPT)
    assert TRANSCRIPT not in _dump(spans)
    assert _meta(timeout, "outcome") == "timeout"
    assert _meta(timeout, "reason_code") == "ASR_TIMEOUT"
    assert _meta(timeout, "http_status") == 504


def test_format_hotwords_strips_list_syntax_and_bounds() -> None:
    from gateway.asr import MAX_HOTWORDS, format_hotwords

    assert format_hotwords(["Ah Ma", "ah ma", "Mum"]) == "Ah Ma|10,Mum|10"
    assert format_hotwords(["Evil|99,Other"]) == "Evil 99 Other|10"
    assert format_hotwords(["", "   ", "x" * 31]) == ""
    assert format_hotwords([f"name{i}" for i in range(100)]).count("|") == MAX_HOTWORDS
