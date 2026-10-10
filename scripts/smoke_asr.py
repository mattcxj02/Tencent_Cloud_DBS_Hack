#!/usr/bin/env python3
"""Smoke test: TRTC ASR sentence recognition via the trtc-asr Python SDK.

Steps:
1. Generate a short synthetic 16 kHz mono 16-bit WAV of English speech
   using macOS `say`.
2. Send it to TRTC ASR using trtc_asr.v3.SentenceRecognizer
   (engine bigmodel, language en).
3. Print the transcript.

Credentials (SDKAppID, SDKSecretKey) are read at runtime only via
pydantic-settings. No secrets are printed. The generated WAV is written
to a gitignored path and cleaned up afterward.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from shared.settings import load_gateway_settings


def _generate_wav(text: str, output_path: Path) -> bool:
    """Generate a 16 kHz mono 16-bit WAV using macOS `say`."""
    try:
        subprocess.run(
            [
                "say",
                "-o",
                str(output_path),
                "--data-format=LEI16@16000",
                text,
            ],
            check=True,
            capture_output=True,
        )
        return output_path.exists()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"[FAILED] Could not generate WAV with `say`: {exc}")
        return False


def _run_asr(wav_path: Path, sdk_app_id: str, sdk_secret_key: str, asr_host: str) -> dict:
    """Send WAV to TRTC ASR v3 sentence recognition and return a result dict."""
    from trtc_asr import v3
    from trtc_asr.errors import ASRError

    result: dict = {"host": asr_host, "engine": "bigmodel", "language": "en"}
    audio_data = wav_path.read_bytes()
    result["audio_bytes"] = len(audio_data)

    recognizer = v3.SentenceRecognizer(v3.new_credential(int(sdk_app_id), sdk_secret_key))
    recognizer.set_endpoint(f"https://{asr_host}")
    req = v3.TranscribeRequest(engine_model_type="bigmodel", voice_format="wav", language="en")
    try:
        resp = recognizer.recognize_data_with_options(audio_data, req)
    except ASRError as exc:
        result["error_class"] = type(exc).__name__
        result["error_message"] = str(exc)[:300]
        return result

    result["request_id"] = resp.request_id
    result["audio_duration_ms"] = resp.audio_duration
    result["detected_language"] = resp.language
    result["transcript"] = resp.result
    return result


def main() -> None:
    print("=" * 60)
    print("SMOKE: TRTC ASR sentence recognition (trtc-asr SDK)")
    print("=" * 60)

    settings = load_gateway_settings()

    if not settings.trtc_enabled:
        print("\n[BLOCKER] TRTC credentials not configured (SDKAppID/SDKSecretKey).")
        print("  Set SDKAppID and SDKSecretKey in .env to run this smoke.")
        sys.exit(1)

    print(f"\nASR host: {settings.trtc_asr_host}")
    print("SDKAppID: …REDACTED")
    print("SDKSecretKey: …REDACTED")

    # ── Step 1: Generate synthetic WAV ──
    print("\n--- Step 1: Generate synthetic 16 kHz mono WAV ---")
    wav_path = Path(tempfile.gettempdir()) / "dcta_smoke_asr.wav"
    text = "send fifty dollars to John"
    if not _generate_wav(text, wav_path):
        print("[BLOCKER] Could not generate test audio. macOS `say` is required.")
        sys.exit(1)

    wav_size = wav_path.stat().st_size
    print(f"Generated: {wav_path} ({wav_size} bytes)")
    print(f"Prompt text: \"{text}\"")

    # ── Step 2: Send to TRTC ASR ──
    print("\n--- Step 2: Send WAV to TRTC ASR ---")
    result = _run_asr(
        wav_path,
        settings.trtc_sdk_app_id,
        settings.trtc_sdk_secret_key,
        settings.trtc_asr_host,
    )

    # ── Step 3: Print transcript ──
    print("\n--- Step 3: Transcript ---")
    transcript = result.get("transcript", "")
    if "error_class" in result:
        print(f"[FAILED] {result['error_class']}: {result.get('error_message', '')}")
    else:
        print(f"Transcript: {transcript}")

    print("\n--- Full result (secrets redacted) ---")
    print(json.dumps(result, indent=2, default=str))

    # Cleanup
    if wav_path.exists():
        wav_path.unlink()

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if "error_class" in result:
        print(f"ASR: FAILED ({result['error_class']})")
    elif transcript:
        print("ASR: OK")
        print(f"  Transcript: {transcript}")
    else:
        print("ASR: AMBIGUOUS (no error but no transcript text)")


if __name__ == "__main__":
    main()
