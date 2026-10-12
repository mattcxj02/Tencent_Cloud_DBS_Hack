"""Own local development service groups and stop only this invocation's children."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".dev"


def stop(runtime: Path = RUNTIME) -> None:
    """Ask this checkout's supervisor to stop; never signal a PID read from disk."""
    try:
        lock = (runtime / "lock").open("r")
    except FileNotFoundError:
        print("No development services running for this checkout.")
        return
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            (runtime / "stop").touch()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    time.sleep(0.1)
            raise RuntimeError("Development supervisor did not finish cleanup") from None
        print("No development services running for this checkout.")


def run_services(commands: list[list[str]], runtime: Path = RUNTIME) -> int:
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    children: list[subprocess.Popen] = []
    stopping = False

    def request_stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    with (runtime / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Development services already running for this checkout.", file=sys.stderr)
            return 1
        path = runtime / "stop"
        path.unlink(missing_ok=True)
        previous = {
            sig: signal.signal(sig, request_stop)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            for command in commands:
                children.append(subprocess.Popen(command, cwd=ROOT, start_new_session=True))
            while not stopping and not path.exists():
                if any(child.poll() is not None for child in children):
                    return 1
                time.sleep(0.1)
            return 0
        finally:
            # Each child created its own session; reload workers inherit that group.
            for child in children:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            for child in children:
                with suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=max(0, deadline - time.monotonic()))
            for child in children:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            path.unlink(missing_ok=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main() -> int:
    if sys.argv[1:] == ["stop"]:
        stop()
        return 0
    # Use the already-selected interpreter, including uv's virtual environment.
    print("Starting agent (8001), gateway (8002), validator (8003)…", flush=True)
    return run_services(
        [
            [sys.executable, "-m", "uvicorn", f"{service}.app:app", "--port", str(port), "--reload"]
            for service, port in (("agent", 8001), ("gateway", 8002), ("validator", 8003))
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
