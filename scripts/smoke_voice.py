#!/usr/bin/env python3
"""Smoke test (T-12): the gateway's voice path against real TRTC sentence ASR.

For each synthetic phrase:
1. Generate a 16 kHz mono 16-bit WAV with macOS ``say``.
2. Run it through ``gateway.asr.validate_wav`` (the same checks the upload route uses).
3. Transcribe the exact bytes with ``gateway.asr.TrtcTranscriber`` (``trtc_asr.v3``,
   international host, engine ``bigmodel``, language ``en``) built from gateway settings.
4. Print the transcript, TRTC request ID, latency and the SHA-256 of the bytes sent.

Credentials (SDKAppID, SDKSecretKey) are read at runtime by pydantic-settings and never
printed. WAV files go to a temporary directory that is deleted afterwards. Transcripts are
reported as returned; ASR accuracy is probabilistic.

Usage: ``uv run python scripts/smoke_voice.py``
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from gateway.asr import AsrError, AudioRejected, TrtcTranscriber, validate_wav
from shared.settings import load_gateway_settings

PHRASES = [
    "Send fifty dollars to Mum from my savings",
    "Pay Ah Ma one hundred dollars",
]


def _say(text: str, path: Path) -> bytes:
    subprocess.run(
        ["say", "-o", str(path), "--data-format=LEI16@16000", text],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def main() -> int:
    print("=" * 64)
    print("SMOKE: T-12 voice path (validate_wav + TrtcTranscriber → TRTC)")
    print("=" * 64)
    settings = load_gateway_settings()
    if not settings.trtc_enabled:
        print("[BLOCKER] TRTC credentials not configured (SDKAppID/SDKSecretKey).")
        return 1
    transcriber = TrtcTranscriber.from_settings(settings)
    print(f"ASR host: {settings.trtc_asr_host}  engine: bigmodel  language: en")
    print("SDKAppID / SDKSecretKey: …REDACTED")

    failures = 0
    with tempfile.TemporaryDirectory(prefix="dcta_voice_") as tmp:
        for i, phrase in enumerate(PHRASES, 1):
            print(f"\n--- Clip {i}: said {phrase!r} ---")
            try:
                wav = _say(phrase, Path(tmp) / f"clip{i}.wav")
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"[BLOCKER] macOS `say` failed: {type(exc).__name__}")
                return 1
            try:
                info = validate_wav(wav)
            except AudioRejected as exc:
                print(f"[FAILED] validate_wav rejected the clip: {exc.code}")
                failures += 1
                continue
            print(
                f"WAV: {len(wav)} bytes, {info.sample_rate} Hz, {info.channels} ch, "
                f"{info.bits_per_sample}-bit, {info.duration_ms} ms"
            )
            print(f"sha256(sent bytes): {hashlib.sha256(wav).hexdigest()}")
            try:
                result = transcriber.transcribe(wav)
            except AsrError as exc:
                print(
                    f"[FAILED] {exc.code} (provider code {exc.provider_code}, {exc.latency_ms} ms)"
                )
                failures += 1
                continue
            print(f"Transcript:        {result.text!r}")
            print(f"TRTC request_id:   {result.request_id}")
            print(f"Latency:           {result.latency_ms} ms (wall clock, incl. network)")
            print(f"TRTC duration:     {result.audio_duration_ms} ms")
            if not result.text.strip():
                print("[NOTE] empty transcript → the route would return ASR_NO_SPEECH")

    print("\n" + "=" * 64)
    print("SUMMARY: " + ("OK" if failures == 0 else f"{failures} clip(s) failed"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
