"""Push-to-talk voice input: WAV checks and TRTC sentence ASR (T-12). Gateway only.

Speech is only an input channel. The browser uploads one 16 kHz mono 16-bit PCM
WAV clip; the gateway validates it here, stores those exact bytes as audio evidence
(``ConversationStore.upload_audio``), sends the same stored bytes to Tencent TRTC
sentence recognition (``trtc_asr.v3.SentenceRecognizer``, international host,
engine ``bigmodel``, language ``en``), and appends the transcript as one
``user_asr_final`` turn. The user can correct it with ``/transcript-edit``.

TRTC credentials (SDKAppID + SDKSecretKey, never the cloud API SecretKey) come from
:class:`shared.settings.GatewaySettings` and never leave this module: errors carry a
stable code and the provider's numeric code only, never the SDK message, the audio
or a secret.

The SDK call is blocking (``urllib``). It runs in the request thread of a sync route
with the SDK's socket timeout (:data:`DEFAULT_ASR_TIMEOUT_S`).

Tests replace the transcriber with :func:`_set_transcriber` (no HTTP or env switch).
"""

from __future__ import annotations

import sqlite3
import threading
import time
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "ASR_ENGINE",
    "ASR_LANGUAGE",
    "ASR_VOICE_FORMAT",
    "WAV_SAMPLE_RATE",
    "WAV_CHANNELS",
    "WAV_BITS_PER_SAMPLE",
    "MAX_VOICE_BYTES",
    "MAX_VOICE_DURATION_MS",
    "ACCEPTED_CONTENT_TYPES",
    "DEFAULT_ASR_TIMEOUT_S",
    "ASR_HTTP_STATUS",
    "AudioRejected",
    "AsrError",
    "AsrResult",
    "WavInfo",
    "Transcriber",
    "TrtcTranscriber",
    "DuplicateSegment",
    "validate_wav",
    "check_content_type",
    "classify_asr_exception",
    "append_voice_turn",
    "find_linked_duplicate",
    "get_transcriber",
    "_set_transcriber",
]

# ── Contract constants ───────────────────────────────────────────────

ASR_ENGINE = "bigmodel"
ASR_LANGUAGE = "en"
ASR_VOICE_FORMAT = "wav"

WAV_SAMPLE_RATE = 16_000
WAV_CHANNELS = 1
WAV_BITS_PER_SAMPLE = 16

#: TRTC sentence recognition limits: 3 MB and 60 seconds per clip.
MAX_VOICE_BYTES = 3 * 1024 * 1024
MAX_VOICE_DURATION_MS = 60_000

#: Multipart part types accepted for the ``audio`` field. The bytes are still parsed:
#: a matching content type alone is never trusted.
ACCEPTED_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})

#: Socket timeout for one TRTC request (connect and each read).
DEFAULT_ASR_TIMEOUT_S = 20.0

_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_PCM {00000001-0000-0010-8000-00aa00389b71}
_PCM_SUBFORMAT_GUID = bytes.fromhex("0100000000001000800000aa00389b71")

# TRTC codes: SDK-local 10xx; v3 server 4xxx/5xxx. 4002 = authentication failure.
_SDK_ERR_CONNECT_FAILED = 1002
_SDK_ERR_AUTH_FAILED = 1005
_SDK_ERR_TIMEOUT = 1006
_SERVER_AUTH_CODES = frozenset({4002})


# ── Errors ───────────────────────────────────────────────────────────


class AudioRejected(Exception):
    """The upload is not an acceptable clip. Nothing is stored or sent to TRTC.

    Codes: ``AUDIO_MISSING``, ``AUDIO_EMPTY``, ``AUDIO_TOO_LARGE``, ``AUDIO_TOO_LONG``,
    ``AUDIO_NOT_WAV``, ``AUDIO_NOT_PCM``, ``AUDIO_NOT_16BIT``, ``AUDIO_NOT_MONO``,
    ``AUDIO_NOT_16KHZ``.
    """

    telemetry_status = "rejected"

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = code
        self.message = message
        self.http_status = http_status


class AsrError(Exception):
    """TRTC could not produce a transcript. Stored audio is kept; no turn is appended.

    ``code``: ``ASR_UNAVAILABLE`` (provider/network/config), ``ASR_TIMEOUT``,
    ``ASR_AUTH_FAILED``, ``ASR_NO_SPEECH`` (empty transcript).
    ``provider_code`` is the numeric TRTC code when known. The SDK message is never kept:
    it can echo request details.
    """

    def __init__(
        self,
        code: str,
        *,
        provider_code: int | None = None,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason_code = code
        self.provider_code = provider_code
        self.latency_ms = latency_ms
        self.request_id = request_id

    @property
    def telemetry_status(self) -> str:
        if self.code == "ASR_TIMEOUT":
            return "timeout"
        if self.code == "ASR_NO_SPEECH":
            return "rejected"
        return "error"


#: HTTP status for each ASR failure code.
ASR_HTTP_STATUS = {
    "ASR_UNAVAILABLE": 502,
    "ASR_AUTH_FAILED": 502,
    "ASR_TIMEOUT": 504,
    "ASR_NO_SPEECH": 422,
}


class DuplicateSegment(Exception):
    """This clip (same audio ID, or the same bytes in this conversation) already has
    its final transcript turn. Carries the existing ``audio_id`` and ``turn_id``."""

    code = "AUDIO_DUPLICATE"

    def __init__(self, audio_id: str, turn_id: str) -> None:
        super().__init__(self.code)
        self.audio_id = audio_id
        self.turn_id = turn_id


# ── WAV validation ───────────────────────────────────────────────────


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    data_bytes: int
    duration_ms: int


def check_content_type(content_type: str | None) -> None:
    """Reject a multipart part whose declared type is not a WAV type."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base not in ACCEPTED_CONTENT_TYPES:
        raise AudioRejected("AUDIO_NOT_WAV", "audio must be uploaded as audio/wav", http_status=415)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def validate_wav(data: bytes) -> WavInfo:
    """Parse a RIFF/WAVE header and enforce the voice contract.

    Accepts only uncompressed PCM, 16-bit, mono, 16 kHz, 1 sample to 60 s, at most 3 MB.
    Chunks before ``data`` (``JUNK``, ``LIST``, ``FLLR``…) are skipped; ``fmt `` must
    come first. ``WAVE_FORMAT_EXTENSIBLE`` is accepted only with the PCM sub-format.
    Raises :class:`AudioRejected` with a stable code.
    """
    if not data:
        raise AudioRejected("AUDIO_EMPTY", "audio upload is empty", http_status=400)
    if len(data) > MAX_VOICE_BYTES:
        raise AudioRejected(
            "AUDIO_TOO_LARGE", f"audio exceeds {MAX_VOICE_BYTES} bytes", http_status=413
        )
    not_wav = AudioRejected("AUDIO_NOT_WAV", "audio is not a valid WAV file", http_status=415)
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise not_wav
    if _u32(data, 4) + 8 > len(data):  # truncated upload
        raise not_wav

    offset = 12
    fmt: tuple[int, int, int, int, int, int] | None = None
    data_bytes: int | None = None
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = _u32(data, offset + 4)
        body = offset + 8
        if body + size > len(data):
            raise not_wav  # chunk runs past the end of the file
        if chunk_id == b"fmt ":
            if fmt is not None or size < 16:
                raise not_wav
            audio_format = _u16(data, body)
            channels = _u16(data, body + 2)
            rate = _u32(data, body + 4)
            byte_rate = _u32(data, body + 8)
            block_align = _u16(data, body + 12)
            bits = _u16(data, body + 14)
            if audio_format == _WAVE_FORMAT_EXTENSIBLE:
                if size < 40 or data[body + 24 : body + 40] != _PCM_SUBFORMAT_GUID:
                    raise AudioRejected(
                        "AUDIO_NOT_PCM", "audio must be uncompressed PCM", http_status=415
                    )
                audio_format = _WAVE_FORMAT_PCM
            fmt = (audio_format, channels, rate, byte_rate, block_align, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise not_wav
            data_bytes = size
            break
        offset = body + size + (size & 1)

    if fmt is None or data_bytes is None:
        raise not_wav
    audio_format, channels, rate, byte_rate, block_align, bits = fmt
    if audio_format != _WAVE_FORMAT_PCM:
        raise AudioRejected("AUDIO_NOT_PCM", "audio must be uncompressed PCM", http_status=415)
    if bits != WAV_BITS_PER_SAMPLE:
        raise AudioRejected("AUDIO_NOT_16BIT", "audio must be 16-bit PCM", http_status=415)
    if channels != WAV_CHANNELS:
        raise AudioRejected("AUDIO_NOT_MONO", "audio must be mono", http_status=415)
    if rate != WAV_SAMPLE_RATE:
        raise AudioRejected("AUDIO_NOT_16KHZ", "audio must be sampled at 16 kHz", http_status=415)
    frame = channels * bits // 8
    if block_align != frame or byte_rate != rate * frame or data_bytes % frame:
        raise not_wav
    if data_bytes == 0:
        raise AudioRejected("AUDIO_EMPTY", "audio contains no samples", http_status=400)
    duration_ms = data_bytes * 1000 // (rate * frame)
    if data_bytes * 1000 > MAX_VOICE_DURATION_MS * rate * frame:
        raise AudioRejected(
            "AUDIO_TOO_LONG", f"audio exceeds {MAX_VOICE_DURATION_MS // 1000} s", http_status=413
        )
    return WavInfo(rate, channels, bits, data_bytes, duration_ms)


# ── Transcription ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AsrResult:
    text: str
    request_id: str
    audio_duration_ms: int
    latency_ms: int


class Transcriber(Protocol):
    """Anything that turns WAV bytes into an :class:`AsrResult` or raises :class:`AsrError`."""

    @property
    def ready(self) -> bool: ...

    def transcribe(self, wav_bytes: bytes, hotwords: Sequence[str] = ()) -> AsrResult: ...


def _is_timeout(exc: BaseException) -> bool:
    """True if *exc* or anything in its cause/context chain is a socket timeout."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        if isinstance(current, urllib.error.URLError) and isinstance(current.reason, TimeoutError):
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_asr_exception(exc: BaseException) -> tuple[str, int | None]:
    """Map an SDK/transport exception to ``(stable code, provider code or None)``."""
    code = getattr(exc, "code", None)
    provider = code if isinstance(code, int) and not isinstance(code, bool) else None
    if provider in _SERVER_AUTH_CODES or provider == _SDK_ERR_AUTH_FAILED:
        return "ASR_AUTH_FAILED", provider
    if provider == _SDK_ERR_TIMEOUT or _is_timeout(exc):
        return "ASR_TIMEOUT", provider
    return "ASR_UNAVAILABLE", provider


def _default_recognizer_factory(sdk_app_id: int, secret: str, host: str, timeout_s: float) -> Any:
    from trtc_asr import v3

    recognizer = v3.SentenceRecognizer(v3.new_credential(sdk_app_id, secret))
    recognizer.set_endpoint(f"https://{host}")
    recognizer.set_timeout(timeout_s)
    return recognizer


def _default_request_factory() -> Any:
    from trtc_asr import v3

    return v3.TranscribeRequest(
        engine_model_type=ASR_ENGINE, voice_format=ASR_VOICE_FORMAT, language=ASR_LANGUAGE
    )


class TrtcTranscriber:
    """TRTC v3 sentence recognition, exactly as ``scripts/smoke_asr.py`` calls it.

    ``recognizer_factory`` / ``request_factory`` exist for tests (a fake SDK object);
    production uses the installed ``trtc_asr`` 2.0.0 SDK.
    """

    def __init__(
        self,
        sdk_app_id: str | None,
        sdk_secret_key: str | None,
        host: str,
        *,
        timeout_s: float = DEFAULT_ASR_TIMEOUT_S,
        recognizer_factory: Callable[[int, str, str, float], Any] | None = None,
        request_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._app_id = sdk_app_id
        self._secret = sdk_secret_key
        self._host = host
        self._timeout_s = timeout_s
        self._recognizer_factory = recognizer_factory or _default_recognizer_factory
        self._request_factory = request_factory or _default_request_factory

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> TrtcTranscriber:
        return cls(
            settings.trtc_sdk_app_id,
            settings.trtc_sdk_secret_key,
            settings.trtc_asr_host,
            **kwargs,
        )

    def __repr__(self) -> str:  # never show credentials
        return f"TrtcTranscriber(host={self._host!r}, ready={self.ready})"

    @property
    def ready(self) -> bool:
        return bool(self._app_id and self._secret and str(self._app_id).strip().isdigit())

    def transcribe(self, wav_bytes: bytes, hotwords: Sequence[str] = ()) -> AsrResult:
        if not self.ready:
            raise AsrError("ASR_UNAVAILABLE")
        started = time.monotonic()
        try:
            recognizer = self._recognizer_factory(
                int(str(self._app_id).strip()), str(self._secret), self._host, self._timeout_s
            )
            request = self._request_factory()
            hotword_list = format_hotwords(hotwords)
            if hotword_list:
                request.hotword_list = hotword_list
            response = recognizer.recognize_data_with_options(wav_bytes, request)
        except Exception as exc:  # noqa: BLE001 - every SDK/transport failure maps to a code
            code, provider = classify_asr_exception(exc)
            raise AsrError(
                code,
                provider_code=provider,
                latency_ms=_elapsed_ms(started),
            ) from None  # drop the SDK message and traceback context
        latency_ms = _elapsed_ms(started)
        request_id = str(getattr(response, "request_id", "") or "")
        duration = getattr(response, "audio_duration", 0)
        text = getattr(response, "result", "")
        return AsrResult(
            text=text.strip() if isinstance(text, str) else "",
            request_id=request_id,
            audio_duration_ms=duration if isinstance(duration, int) else 0,
            latency_ms=latency_ms,
        )


#: TRTC hotword weight and bounds. "Ah Ma" is heard as "Amma" without a hotword.
HOTWORD_WEIGHT = 10
MAX_HOTWORDS = 30
MAX_HOTWORD_CHARS = 30


def format_hotwords(words: Sequence[str]) -> str:
    """TRTC ``hotword_list`` ("word|weight,...") from the user's own bank names.

    Separators are stripped from each word, words are trimmed, deduplicated
    case-insensitively and bounded, so bank labels cannot inject list syntax.
    """
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        clean = " ".join(str(word).replace("|", " ").replace(",", " ").split())
        if not clean or len(clean) > MAX_HOTWORD_CHARS or clean.casefold() in seen:
            continue
        seen.add(clean.casefold())
        out.append(f"{clean}|{HOTWORD_WEIGHT}")
        if len(out) == MAX_HOTWORDS:
            break
    return ",".join(out)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ── Module wiring (tests replace it; HTTP cannot) ────────────────────

_transcriber: Transcriber | None = None
_wiring_lock = threading.Lock()


def get_transcriber() -> Transcriber:
    """The process-wide TRTC transcriber built from gateway settings."""
    global _transcriber
    with _wiring_lock:
        if _transcriber is None:
            from shared.settings import load_gateway_settings

            _transcriber = TrtcTranscriber.from_settings(load_gateway_settings())
        return _transcriber


def _set_transcriber(transcriber: Transcriber | None) -> None:
    """Test hook: inject a fake transcriber (``None`` restores the settings default)."""
    global _transcriber
    _transcriber = transcriber


# ── Final segment append (one clip → at most one turn) ──────────────


def find_linked_duplicate(
    conn: sqlite3.Connection, conversation_id: str, digest: str, *, exclude_audio_id: str = ""
) -> tuple[str, str] | None:
    """Return ``(audio_id, turn_id)`` of a clip with these exact bytes in this conversation
    that already produced its final turn, else None."""
    row = conn.execute(
        "SELECT audio_id, turn_id FROM audio_evidence "
        "WHERE conversation_id = ? AND sha256_digest = ? AND turn_id IS NOT NULL "
        "AND audio_id != ? ORDER BY created_at LIMIT 1",
        (conversation_id, digest, exclude_audio_id),
    ).fetchone()
    return (row["audio_id"], row["turn_id"]) if row is not None else None


def append_voice_turn(
    store: Any,
    user_id: str,
    conversation_id: str,
    audio_id: str,
    text: str,
    *,
    audit_payload: dict[str, Any],
    trace_id: str | None = None,
    span_id: str | None = None,
) -> dict[str, Any]:
    """Append the clip's ``user_asr_final`` turn and link it to the audio, atomically.

    One ``BEGIN IMMEDIATE`` transaction: owner check; the audio row must belong to this
    conversation and have no turn yet, and no other clip with the same digest in this
    conversation may already have one (else :class:`DuplicateSegment`); context bounds
    (``ContextLimitError``); append + revision bump + supersession (the store's own
    helper); set ``audio_evidence.turn_id``; audit ``gateway.voice.transcribed``.

    Uses ConversationStore internals so the link and the turn commit together.
    """
    from gateway.audit import append_audit

    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conv = store._get_conversation_row(conn, conversation_id)
        if conv is None or conv["owner_user_id"] != user_id:
            raise PermissionError("conversation not owned by user")
        audio = conn.execute(
            "SELECT turn_id, sha256_digest FROM audio_evidence "
            "WHERE audio_id = ? AND conversation_id = ?",
            (audio_id, conversation_id),
        ).fetchone()
        if audio is None:
            raise ValueError(f"audio not found: {audio_id}")
        if audio["turn_id"] is not None:
            raise DuplicateSegment(audio_id, audio["turn_id"])
        duplicate = find_linked_duplicate(
            conn, conversation_id, audio["sha256_digest"], exclude_audio_id=audio_id
        )
        if duplicate is not None:
            raise DuplicateSegment(*duplicate)
        store._check_bounds(conn, conversation_id, text)
        turn = store._append_turn(conn, conversation_id, "user_asr_final", text, bump_revision=True)
        linked = conn.execute(
            "UPDATE audio_evidence SET turn_id = ? WHERE audio_id = ? AND turn_id IS NULL",
            (turn["turn_id"], audio_id),
        ).rowcount
        if linked != 1:
            raise RuntimeError("audio link failed")
        append_audit(
            conn,
            source="gateway",
            conversation_id=conversation_id,
            user_id=user_id,
            step="gateway.voice.transcribed",
            payload={
                **audit_payload,
                "audio_id": audio_id,
                "sha256_digest": audio["sha256_digest"],
                "turn_id": turn["turn_id"],
                "conversation_revision": turn["conversation_revision"],
            },
            trace_id=trace_id,
            span_id=span_id,
        )
        conn.commit()
        return turn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
