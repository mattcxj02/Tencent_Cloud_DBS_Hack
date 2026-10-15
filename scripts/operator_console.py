#!/usr/bin/env python3
"""Operator console for the SIMULATED step-up phone channel (T-07 demo only).

Stands in for "the bank texts you a code". It talks to the gateway's operator routes
with ``X-Operator-Token`` = ``OPERATOR_TOKEN`` (read from the process environment,
never printed) and nothing else: no session cookie, no ``Authorization`` header, no
``.netrc``/proxy credentials (``trust_env=False``). The gateway refuses operator
requests that carry a session cookie or an agent token anyway.

Usage::

    OPERATOR_TOKEN=... uv run python scripts/operator_console.py list
    OPERATOR_TOKEN=... uv run python scripts/operator_console.py code <authorization_id>
    OPERATOR_TOKEN=... uv run python scripts/operator_console.py code   # the only waiting one
    # optional: --base-url http://localhost:8002 (default)

``list`` shows challenges waiting for a code (never the codes). ``code`` "delivers" the
code for one authorization (repeatable; the expiry is never extended) and prints it.
Without an ID it delivers the code only when exactly one challenge is waiting; with none
or several it prints the list and asks for the ID, so it never picks between payments.
Exit codes: 0 ok, 1 gateway refused or unreachable, 2 usage/configuration error.
Not production MFA: no SMS, push or bank channel is involved.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

import httpx

DEFAULT_BASE_URL = "http://localhost:8002"
TOKEN_ENV = "OPERATOR_TOKEN"
TOKEN_HEADER = "X-Operator-Token"
MIN_TOKEN_LEN = 32
_AUTHZ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ConsoleError(Exception):
    """A refusal or transport failure, as a short safe message."""


def operator_token(environ: Mapping[str, str] | None = None) -> str | None:
    """``OPERATOR_TOKEN`` from the environment if it is long enough, else None."""
    value = (environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip()
    return value if len(value) >= MIN_TOKEN_LEN else None


def make_client(base_url: str = DEFAULT_BASE_URL) -> httpx.Client:
    """A cookie-less client that ignores ambient credentials (.netrc, proxies)."""
    return httpx.Client(base_url=base_url, timeout=10.0, follow_redirects=False, trust_env=False)


def _request(client: httpx.Client, method: str, path: str, token: str) -> dict[str, Any]:
    try:
        response = client.request(
            method, path, headers={TOKEN_HEADER: token, "Accept": "application/json"}
        )
    except httpx.HTTPError as exc:
        raise ConsoleError(f"gateway unreachable ({type(exc).__name__})") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise ConsoleError(f"HTTP {response.status_code}: {detail or 'refused'}")
    if not isinstance(body, dict):
        raise ConsoleError("unexpected response")
    return body


def list_pending(client: httpx.Client, token: str) -> dict[str, Any]:
    """``GET /operator/step-up``: challenges awaiting a code (no codes)."""
    return _request(client, "GET", "/operator/step-up", token)


def deliver(client: httpx.Client, token: str, authorization_id: str) -> dict[str, Any]:
    """``POST /operator/step-up/{id}/deliver``: the simulated delivery, with the code."""
    if not _AUTHZ_RE.match(authorization_id):
        raise ConsoleError("invalid authorization id")
    return _request(client, "POST", f"/operator/step-up/{authorization_id}/deliver", token)


def deliver_only_pending(client: httpx.Client, token: str, out: TextIO) -> dict[str, Any]:
    """The code for the single waiting challenge; with none or several, list and refuse."""
    body = list_pending(client, token)
    challenges = body.get("challenges") or []
    if len(challenges) == 1 and isinstance(challenges[0].get("authorization_id"), str):
        return deliver(client, token, challenges[0]["authorization_id"])
    _print_list(body, out)
    if not challenges:
        raise ConsoleError("no step-up code is waiting (approve a payment first)")
    raise ConsoleError("several codes are waiting; run: code <authorization_id>")


def _print_list(body: dict[str, Any], out: TextIO) -> None:
    print(body.get("simulation", "SIMULATED channel"), file=out)
    challenges = body.get("challenges") or []
    if not challenges:
        print("No step-up challenges are waiting.", file=out)
        return
    for c in challenges:
        print(
            f"- {c.get('authorization_id')}  user={c.get('user_id')}  "
            f"expires={c.get('expires_at')}  attempts_left={c.get('attempts_remaining')}  "
            f"deliveries={c.get('deliveries')}",
            file=out,
        )


def _print_code(body: dict[str, Any], out: TextIO) -> None:
    print(body.get("simulation", "SIMULATED channel"), file=out)
    print(
        f"Simulated message to {body.get('user_id')}: your DCTA approval code for "
        f"{body.get('authorization_id')} is {body.get('code')} "
        f"(expires {body.get('expires_at')}, attempts left {body.get('attempts_remaining')}).",
        file=out,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description="Simulated step-up phone channel (demo).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="gateway URL")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="challenges waiting for a code (no codes)")
    code = sub.add_parser("code", help="deliver and print the code for an authorization")
    code.add_argument("authorization_id", nargs="?", help="omit when exactly one code is waiting")
    args = parser.parse_args(argv)

    token = operator_token(environ)
    if token is None:
        print(f"error: set {TOKEN_ENV} (at least {MIN_TOKEN_LEN} characters)", file=err)
        return 2
    http = client or make_client(args.base_url)
    try:
        if args.command == "code" and args.authorization_id is None:
            _print_code(deliver_only_pending(http, token, out), out)
        elif args.command == "code":
            _print_code(deliver(http, token, args.authorization_id), out)
        else:
            _print_list(list_pending(http, token), out)
    except ConsoleError as exc:
        print(f"error: {exc}", file=err)
        return 1
    finally:
        if client is None:
            http.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
