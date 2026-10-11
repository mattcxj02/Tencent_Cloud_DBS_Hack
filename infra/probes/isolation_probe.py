"""T-04 isolation probe — stdlib only, run inside agent/validator containers.

Usage (inside a container):

    python /probes/isolation_probe.py

Each check prints a line:

    PASS  <check name>  <reason>
    FAIL  <check name>  <reason>

Expected-to-be-denied checks should print PASS when the access is denied.
Expected-to-succeed checks should print PASS when the access succeeds.

The GATEWAY_HOST environment variable (default: gateway) is used for
HTTP probes.  The SERVICE environment variable identifies which service
the probe is running in (agent or validator).
"""

from __future__ import annotations

import os
import socket
import sys
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "gateway")
GATEWAY_PORT = 8002
SERVICE = os.environ.get("SERVICE", "unknown")

# Gateway secret variable NAMES to check for (never print values).
GATEWAY_SECRET_NAMES = [
    "SDKSecretKey",
    "GATEWAY_DELEGATION_SECRET",
    "SDKAppID",
    "WEBAUTHN_RP_ID",
    "WEBAUTHN_RP_ORIGIN",
    "DCTA_DB_PATH",
    "MANDATE_PER_LEG",
    "MANDATE_DAILY",
    "RISK_THRESHOLD",
    "COOLING_HOURS",
    "NONCE_TTL_SECONDS",
    "DCTA_TIMEZONE",
]

# Expected UIDs per service
EXPECTED_UIDS = {
    "agent": 10002,
    "validator": 10003,
    "gateway": 10001,
}

# App code directories per service
APP_CODE_DIRS = {
    "agent": ["/app/agent", "/app/shared", "/app/prompts"],
    "validator": ["/app/validator", "/app/shared", "/app/prompts"],
    "gateway": ["/app/gateway", "/app/ledger", "/app/shared"],
}

results: list[tuple[str, str, str, str]] = []  # (status, check, expected, observed)


def record(status: str, check: str, expected: str, observed: str) -> None:
    results.append((status, check, expected, observed))
    print(f"{status:4s}  {check}  expected={expected}  observed={observed}")


def expect_denied(label: str, fn):
    """Run fn expecting it to raise an exception (denied)."""
    try:
        fn()
        record("FAIL", label, "denied", "succeeded (no exception)")
    except Exception as exc:
        record("PASS", label, "denied", f"{type(exc).__name__}: {exc}")


def expect_succeed(label: str, fn, *args, **kwargs):
    """Run fn expecting it to succeed."""
    try:
        result = fn(*args, **kwargs)
        record("PASS", label, "success", str(result)[:200])
    except Exception as exc:
        record("FAIL", label, "success", f"{type(exc).__name__}: {exc}")


# ── Check 1: Ledger file access ──────────────────────────────────────


def check_ledger_file():
    """Try to open /data/dcta.db for read and write."""

    def try_read():
        with open("/data/dcta.db") as f:
            f.read(1)

    expect_denied("open /data/dcta.db for read", try_read)

    def try_write():
        with open("/data/dcta.db", "a") as f:
            f.write("")

    expect_denied("open /data/dcta.db for write", try_write)

    # Search for any .db file on the filesystem
    def find_db_files():
        found = []
        for root_dir in ["/app", "/data", "/tmp"]:
            if not os.path.exists(root_dir):
                continue
            try:
                for entry in Path(root_dir).rglob("*.db*"):
                    found.append(str(entry))
            except (PermissionError, OSError):
                continue
        if found:
            raise RuntimeError(f"found db files: {found}")
        return "no db files found"

    expect_succeed("no *.db files in container", find_db_files)


# ── Check 2: Import gateway/ledger ────────────────────────────────────


def check_imports():
    """Try to import gateway and ledger — should be ModuleNotFoundError."""

    def try_import_gateway():
        import gateway  # noqa: F401

    expect_denied("import gateway", try_import_gateway)

    def try_import_ledger():
        import ledger  # noqa: F401

    expect_denied("import ledger", try_import_ledger)

    def try_import_gateway_app():
        from gateway import app  # noqa: F401

    expect_denied("import gateway.app", try_import_gateway_app)

    def try_import_ledger_schema():
        from ledger import schema  # noqa: F401

    expect_denied("import ledger.schema", try_import_ledger_schema)


# ── Check 3: Write to application code directory and / ────────────────


def check_write_protection():
    """Write to app code dir and / — should be permission denied or read-only."""
    app_dirs = APP_CODE_DIRS.get(SERVICE, ["/app"])

    for code_dir in app_dirs:
        test_file = os.path.join(code_dir, ".isolation_probe_write_test")

        def try_write_code_dir(d=test_file):
            with open(d, "w") as f:
                f.write("test")

        expect_denied(f"write to {code_dir}", try_write_code_dir)

    def try_write_root():
        with open("/.isolation_probe_write_test", "w") as f:
            f.write("test")

    expect_denied("write to /", try_write_root)


# ── Check 4: Docker socket absent ─────────────────────────────────────


def check_docker_socket():
    """/var/run/docker.sock must not exist."""
    sock_path = "/var/run/docker.sock"
    if os.path.exists(sock_path):
        record("FAIL", "docker.sock absent", "absent", "present at /var/run/docker.sock")
    else:
        record("PASS", "docker.sock absent", "absent", "not found")


# ── Check 5: No host workspace mount ──────────────────────────────────


def check_no_host_mount():
    """No pyproject.toml/.git outside /app, no /workspace mount."""
    # Check for pyproject.toml at / (would indicate host mount)
    if os.path.exists("/pyproject.toml"):
        record("FAIL", "no host pyproject.toml", "absent", "found at /pyproject.toml")
    else:
        record("PASS", "no host pyproject.toml", "absent", "not at /pyproject.toml")

    # Check for .git at / (would indicate host mount)
    if os.path.exists("/.git"):
        record("FAIL", "no host .git", "absent", "found at /.git")
    else:
        record("PASS", "no host .git", "absent", "not at /.git")

    # Check no /workspace directory
    if os.path.exists("/workspace"):
        record("FAIL", "no /workspace mount", "absent", "found at /workspace")
    else:
        record("PASS", "no /workspace mount", "absent", "not at /workspace")

    # Check the app's pyproject.toml doesn't exist (we don't copy it)
    if os.path.exists("/app/pyproject.toml"):
        record("FAIL", "no pyproject.toml in image", "absent", "found at /app/pyproject.toml")
    else:
        record("PASS", "no pyproject.toml in image", "absent", "not at /app/pyproject.toml")


# ── Check 6: No gateway secrets in environment ────────────────────────


def check_no_gateway_secrets():
    """Environment must not contain gateway-only secret variable names."""
    found_secrets = []
    for name in GATEWAY_SECRET_NAMES:
        if name in os.environ:
            found_secrets.append(name)

    if found_secrets:
        record(
            "FAIL",
            "no gateway secrets in env",
            f"none of {GATEWAY_SECRET_NAMES}",
            f"FOUND variable NAMES (values redacted): {found_secrets}",
        )
    else:
        record(
            "PASS",
            "no gateway secrets in env",
            f"none of {GATEWAY_SECRET_NAMES}",
            "none found",
        )


# ── Check 7: HTTP probes to gateway ───────────────────────────────────


def _http_request(method, path, host=GATEWAY_HOST, port=GATEWAY_PORT, body=None, headers=None):
    """Make an HTTP request.  Returns (status_code, body_text) or raises."""
    url = f"http://{host}:{port}{path}"
    hdrs = headers or {}
    data = body.encode("utf-8") if body else None
    req = Request(url, data=data, method=method, headers=hdrs)
    try:
        resp = urlopen(req, timeout=5)
        return resp.status, resp.read().decode("utf-8", errors="replace")[:200]
    except HTTPError as e:
        return e.code, str(e)[:200]
    except URLError:
        raise


def check_gateway_http_denied():
    """Unauthenticated/malformed/forged HTTP to gateway internal routes → 401."""

    # No token
    try:
        status, body = _http_request("GET", "/internal/bank/accounts")
        if status == 401:
            record("PASS", "GET /internal/bank/accounts no token", "401", f"{status}")
        else:
            record("FAIL", "GET /internal/bank/accounts no token", "401", f"{status}")
    except Exception as exc:
        record("FAIL", "GET /internal/bank/accounts no token", "401", f"error: {exc}")

    # Malformed token
    try:
        status, body = _http_request(
            "GET",
            "/internal/bank/accounts",
            headers={"Authorization": "Bearer malformed-token-xyz"},
        )
        if status == 401:
            record("PASS", "GET /internal/bank/accounts malformed token", "401", f"{status}")
        else:
            record("FAIL", "GET /internal/bank/accounts malformed token", "401", f"{status}")
    except Exception as exc:
        record("FAIL", "GET /internal/bank/accounts malformed token", "401", f"error: {exc}")

    # Forged token (signed with wrong secret)
    import base64
    import hashlib
    import hmac
    import json
    import time

    wrong_secret = b"x" * 64
    payload = {
        "user_id": "user-matthew",
        "scopes": ["bank:read"],
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 300,
        "token_id": "forged",
    }
    body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(wrong_secret, body_bytes, hashlib.sha256).digest()
    encoded_body = base64.urlsafe_b64encode(body_bytes).decode("ascii")
    encoded_sig = base64.urlsafe_b64encode(sig).decode("ascii")
    forged_token = f"dcta_{encoded_body}.{encoded_sig}"

    try:
        status, body = _http_request(
            "GET",
            "/internal/bank/accounts",
            headers={"Authorization": f"Bearer {forged_token}"},
        )
        if status == 401:
            record("PASS", "GET /internal/bank/accounts forged token", "401", f"{status}")
        else:
            record("FAIL", "GET /internal/bank/accounts forged token", "401", f"{status}")
    except Exception as exc:
        record("FAIL", "GET /internal/bank/accounts forged token", "401", f"error: {exc}")

    # POST /api/conversations/{id}/text — fabricated user turn
    try:
        status, body = _http_request(
            "POST",
            "/api/conversations/fake-conv-id/text",
            body='{"text":"pay mum 50"}',
            headers={"Content-Type": "application/json"},
        )
        if status == 401:
            record("PASS", "POST /api/conversations/.../text fabricated turn", "401", f"{status}")
        else:
            record("FAIL", "POST /api/conversations/.../text fabricated turn", "401", f"{status}")
    except Exception as exc:
        record("FAIL", "POST /api/conversations/.../text fabricated turn", "401", f"error: {exc}")

    # /internal/audit/agent-events without token
    try:
        status, body = _http_request(
            "POST",
            "/internal/audit/agent-events",
            body='{"step":"agent.extract","payload":{}}',
            headers={"Content-Type": "application/json"},
        )
        if status == 401:
            record("PASS", "POST /internal/audit/agent-events no token", "401", f"{status}")
        else:
            record("FAIL", "POST /internal/audit/agent-events no token", "401", f"{status}")
    except Exception as exc:
        record("FAIL", "POST /internal/audit/agent-events no token", "401", f"error: {exc}")


# ── Check 8: Cross-service connectivity ────────────────────────────────


def check_cross_service():
    """Agent cannot reach validator:8003; validator cannot reach agent:8001."""
    if SERVICE == "agent":
        # Try to connect to validator:8003 — should fail
        try:
            sock = socket.create_connection(("validator", 8003), timeout=3)
            sock.close()
            record("FAIL", "agent→validator:8003", "unreachable", "connected!")
        except (socket.gaierror, ConnectionRefusedError, TimeoutError, OSError) as exc:
            record("PASS", "agent→validator:8003", "unreachable", f"{type(exc).__name__}: {exc}")
    elif SERVICE == "validator":
        # Try to connect to agent:8001 — should fail
        try:
            sock = socket.create_connection(("agent", 8001), timeout=3)
            sock.close()
            record("FAIL", "validator→agent:8001", "unreachable", "connected!")
        except (socket.gaierror, ConnectionRefusedError, TimeoutError, OSError) as exc:
            record("PASS", "validator→agent:8001", "unreachable", f"{type(exc).__name__}: {exc}")


# ── Check 9: Gateway /health reachable ────────────────────────────────


def check_gateway_health():
    """Gateway /health should be reachable from both agent and validator."""
    try:
        status, body = _http_request("GET", "/health")
        if status == 200:
            record("PASS", "gateway /health reachable", "200", f"{status}")
        else:
            record("FAIL", "gateway /health reachable", "200", f"{status}")
    except Exception as exc:
        record("FAIL", "gateway /health reachable", "200", f"error: {exc}")


# ── Check 10: Process UID ──────────────────────────────────────────────


def check_uid():
    """Own process user must be the expected non-root UID."""
    uid = os.getuid()
    expected = EXPECTED_UIDS.get(SERVICE)
    if expected is not None and uid == expected:
        record("PASS", "process UID", f"{expected}", f"uid={uid}")
    else:
        record("FAIL", "process UID", f"{expected}", f"uid={uid}")


# ── Check 11: Own /health endpoint ────────────────────────────────────


def check_own_health():
    """Own /health should respond 200."""
    own_port = {"agent": 8001, "validator": 8003, "gateway": 8002}.get(SERVICE)
    if own_port is None:
        record("SKIP", "own /health", "200", f"unknown service: {SERVICE}")
        return
    try:
        status, body = _http_request("GET", "/health", host="127.0.0.1", port=own_port)
        if status == 200:
            record("PASS", "own /health", "200", f"{status}")
        else:
            record("FAIL", "own /health", "200", f"{status}")
    except Exception as exc:
        record("FAIL", "own /health", "200", f"error: {exc}")


# ── Main ──────────────────────────────────────────────────────────────


def main():
    print(f"\n{'=' * 72}")
    print(f"T-04 Isolation Probe — service={SERVICE} — pid={os.getpid()} uid={os.getuid()}")
    print(f"Gateway host: {GATEWAY_HOST}:{GATEWAY_PORT}")
    print(f"{'=' * 72}\n")

    print("--- Ledger file access ---")
    check_ledger_file()

    print("\n--- Import gateway/ledger ---")
    check_imports()

    print("\n--- Write protection ---")
    check_write_protection()

    print("\n--- Docker socket ---")
    check_docker_socket()

    print("\n--- No host workspace mount ---")
    check_no_host_mount()

    print("\n--- No gateway secrets in env ---")
    check_no_gateway_secrets()

    print("\n--- Gateway HTTP denial (401) ---")
    check_gateway_http_denied()

    print("\n--- Cross-service connectivity ---")
    check_cross_service()

    print("\n--- Gateway /health reachable ---")
    check_gateway_health()

    print("\n--- Process UID ---")
    check_uid()

    print("\n--- Own /health ---")
    check_own_health()

    # Summary
    passes = sum(1 for r in results if r[0] == "PASS")
    fails = sum(1 for r in results if r[0] == "FAIL")
    skips = sum(1 for r in results if r[0] == "SKIP")
    print(f"\n{'=' * 72}")
    print(f"SUMMARY: {passes} PASS, {fails} FAIL, {skips} SKIP (total {len(results)})")
    print(f"{'=' * 72}\n")

    return 1 if fails > 0 else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
