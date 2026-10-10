"""CLI entry point for ``make seed``.

Usage::

    make seed                         # uses default DCTA_DB_PATH (data/dcta.db)
    DCTA_DB_PATH=/tmp/test.db make seed   # seed a temp DB
"""

from __future__ import annotations

import sys
from pathlib import Path

from ledger.schema import connect, init_db
from ledger.seed import seed_db
from shared.settings import load_gateway_settings


def main() -> int:
    settings = load_gateway_settings()
    db_path = settings.db_path
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        init_db(conn)
        seed_db(conn)

        # Report counts
        tables = [
            "users",
            "kyc",
            "accounts",
            "account_holders",
            "payees",
            "payee_aliases",
            "billers",
            "saved_billers",
            "cards",
            "holdings",
            "equity_quotes",
            "mandates",
        ]
        print(f"Seeded {db_path}")
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
