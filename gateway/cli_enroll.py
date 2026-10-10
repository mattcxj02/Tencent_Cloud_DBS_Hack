"""Gateway CLI: enrollment code generation.

Usage::

    make enroll-code USER=user-matthew

Prints a one-time enrollment code to stdout.  The code is stored only as
a hash, has a 15-minute expiry, and is single-use.  The caller can never
choose the user ID — it comes only from the ``USER`` argument.
"""

from __future__ import annotations

import sys

from gateway.identity import IdentityStore
from shared.settings import load_gateway_settings


def main() -> None:
    """Generate an enrollment code for the user specified on the command line."""
    if len(sys.argv) < 2 or not sys.argv[1].startswith("USER="):
        print("Usage: make enroll-code USER=<user_id>", file=sys.stderr)
        sys.exit(1)

    user_id = sys.argv[1].split("=", 1)[1]
    if not user_id:
        print("Error: USER must not be empty", file=sys.stderr)
        sys.exit(1)

    settings = load_gateway_settings()
    store = IdentityStore(settings.db_path)

    from ledger.schema import connect

    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            print(f"Error: user {user_id!r} not found in database", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    code = store.create_enrollment_code(user_id)
    print(code)


if __name__ == "__main__":
    main()
