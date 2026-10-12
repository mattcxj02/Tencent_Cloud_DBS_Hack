"""Development shutdown owns its service groups, including reload-like children."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts.dev import stop

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Development process did not reach the expected state")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("shutdown", ["stop", "SIGINT", "SIGTERM", "SIGHUP", "service-exit"])
def test_shutdown_cleans_children_and_keeps_unrelated_process(tmp_path, shutdown):
    runtime = tmp_path / "run"
    child_file = tmp_path / "child.pid"
    worker_file = tmp_path / "worker.pid"
    exit_file = tmp_path / "exit"
    service = tmp_path / "service.py"
    service.write_text("""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

def finish(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(str(os.getpid()))
Path(sys.argv[2]).write_text(str(worker.pid))
try:
    while True:
        time.sleep(0.1)
finally:
    worker.wait(timeout=3)
""")
    commands = [[sys.executable, str(service), str(child_file), str(worker_file)]]
    if shutdown == "service-exit":
        commands.append(
            [
                sys.executable,
                "-c",
                "import time,sys; from pathlib import Path; "
                "\nwhile not Path(sys.argv[1]).exists(): time.sleep(0.05)\n",
                str(exit_file),
            ]
        )
    invocation = (
        "from pathlib import Path; from scripts.dev import run_services; "
        f"raise SystemExit(run_services({commands!r}, Path({str(runtime)!r})))"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    supervisor = subprocess.Popen([sys.executable, "-c", invocation], cwd=ROOT)
    try:
        wait_for(worker_file.exists)
        child = int(child_file.read_text())
        worker = int(worker_file.read_text())
        assert alive(child) and alive(worker)
        if shutdown == "stop":
            stop(runtime)
        elif shutdown == "service-exit":
            exit_file.touch()
        else:
            supervisor.send_signal(getattr(signal, shutdown))
        assert supervisor.wait(timeout=8) == (1 if shutdown == "service-exit" else 0)
        wait_for(lambda: not alive(child) and not alive(worker))
        assert unrelated.poll() is None
        assert not (runtime / "stop").exists()
        stop(runtime)  # repeated shutdown is harmless
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=8)
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_second_invocation_cannot_replace_the_project_supervisor(tmp_path):
    runtime = tmp_path / "run"
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    invocation = (
        "from pathlib import Path; from scripts.dev import run_services; "
        f"raise SystemExit(run_services([{command!r}], Path({str(runtime)!r})))"
    )
    first = subprocess.Popen([sys.executable, "-c", invocation], cwd=ROOT)
    try:
        wait_for(lambda: (runtime / "lock").exists())
        second = subprocess.run([sys.executable, "-c", invocation], cwd=ROOT, timeout=3)
        assert second.returncode == 1
        assert first.poll() is None
        stop(runtime)
        assert first.wait(timeout=8) == 0
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=8)
