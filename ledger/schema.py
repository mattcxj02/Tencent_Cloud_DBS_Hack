"""SQLite schema for the gateway-owned mock bank.

All money is stored as INTEGER minor units (cents), never REAL.
``PRAGMA foreign_keys=ON`` and ``journal_mode=WAL`` are set on every
connection.  Use :func:`init_db` to create or migrate the schema; it is
idempotent (safe to call multiple times).

This module is imported only by gateway code.  The agent and validator
services have no ledger imports (enforced by AST tests in T-02).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shared.money import Money

__all__ = ["SCHEMA_VERSION", "init_db", "connect", "money_to_cents", "cents_to_money"]

#: Current schema version tracked in the ``schema_meta`` table.
#: Bumped to 3 in T-05 (identity, sessions, frozen drafts, authorizations).
#: Bumped to 4 in T-06 (execution: transaction leg columns, receipts keyed by
#: the idempotency key, mandate expiry/max legs, authorization status reason).
SCHEMA_VERSION = 4

#: How long a connection waits for another writer's lock before failing.
BUSY_TIMEOUT_MS = 10_000


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys, WAL and a busy timeout.

    ``row_factory`` is set to :class:`sqlite3.Row` for dict-style access.
    Writers serialize on ``BEGIN IMMEDIATE``; the busy timeout makes a
    concurrent writer wait for the lock instead of failing at once.
    """
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def money_to_cents(m: Money) -> int:
    """Convert :class:`Money` to INTEGER minor units (cents)."""
    return int(m.value * 100)


def cents_to_money(cents: int) -> Money:
    """Convert INTEGER minor units back to :class:`Money`."""
    return Money(f"{cents // 100}.{cents % 100:02d}")


# DDL grouped by table for readability.

_SCHEMA_DDL = """
-- ── Schema version ──
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── Users ──
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ── KYC ──
CREATE TABLE IF NOT EXISTS kyc (
    user_id    TEXT PRIMARY KEY REFERENCES users(user_id),
    status     TEXT NOT NULL CHECK (status IN ('verified', 'unverified', 'expired')),
    tier       INTEGER NOT NULL CHECK (tier IN (1, 2, 3)),
    expires_at TEXT,
    checked_at TEXT NOT NULL
);

-- ── Accounts ──
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    type          TEXT NOT NULL CHECK (type IN ('savings', 'current', 'joint')),
    masked_suffix TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

-- ── Account holders (many-to-many; joint accounts have two rows) ──
CREATE TABLE IF NOT EXISTS account_holders (
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    role       TEXT NOT NULL CHECK (role IN ('owner', 'joint')),
    PRIMARY KEY (user_id, account_id)
);

-- ── Payees ──
CREATE TABLE IF NOT EXISTS payees (
    payee_id      TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(user_id),
    display_name  TEXT NOT NULL,
    bank          TEXT,
    masked_suffix TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('bank', 'paynow')),
    sanctioned    INTEGER NOT NULL DEFAULT 0 CHECK (sanctioned IN (0, 1)),
    created_at    TEXT NOT NULL
);

-- ── Payee aliases (alias text → payee) ──
CREATE TABLE IF NOT EXISTS payee_aliases (
    alias    TEXT PRIMARY KEY,
    payee_id TEXT NOT NULL REFERENCES payees(payee_id),
    owner_user_id TEXT NOT NULL REFERENCES users(user_id)
);

-- ── Billers ──
CREATE TABLE IF NOT EXISTS billers (
    biller_id   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category    TEXT
);

-- ── Saved biller links (per-user saved biller with bill reference) ──
CREATE TABLE IF NOT EXISTS saved_billers (
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    biller_id  TEXT NOT NULL REFERENCES billers(biller_id),
    bill_reference TEXT,
    saved_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, biller_id)
);

-- ── Cards ──
CREATE TABLE IF NOT EXISTS cards (
    card_id          TEXT PRIMARY KEY,
    owner_user_id    TEXT NOT NULL REFERENCES users(user_id),
    masked_suffix    TEXT NOT NULL,
    outstanding_cents INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

-- ── Equity holdings ──
CREATE TABLE IF NOT EXISTS holdings (
    holding_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(user_id),
    symbol     TEXT NOT NULL,
    quantity   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- ── Equity quotes (never mutated; expired stays expired) ──
CREATE TABLE IF NOT EXISTS equity_quotes (
    quote_id         TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    quoted_at        TEXT NOT NULL,
    expires_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equity_quotes_symbol ON equity_quotes(symbol);

-- ── Mandates ──
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id),
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'pending', 'paused')),
    per_leg_cents    INTEGER NOT NULL,
    daily_cents      INTEGER NOT NULL,
    allowed_actions  TEXT NOT NULL,  -- comma-separated Action values
    effective_at     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (user_id, version)
);

CREATE INDEX IF NOT EXISTS idx_mandates_user ON mandates(user_id);

-- ── Transactions (v3 baseline; T-06 adds leg columns in _migrate_to_v4) ──
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(user_id),
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    action         TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);

-- ── Receipts (v3 baseline shape, never written; _migrate_to_v4 replaces it) ──
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id      TEXT PRIMARY KEY,
    transaction_id  TEXT NOT NULL REFERENCES transactions(transaction_id),
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    bundle_id       TEXT,
    payload_hash    TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_user ON receipts(user_id);

-- ════════════════════════════════════════════════════════════════
-- T-03: Gateway-owned conversation store, audio evidence, audit chain
-- ════════════════════════════════════════════════════════════════

-- ── Conversations ──
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id  TEXT PRIMARY KEY,
    owner_user_id    TEXT NOT NULL REFERENCES users(user_id),
    revision         INTEGER NOT NULL DEFAULT 0,
    conversation_hash TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_user_id);

-- ── Conversation turns (ordered, append-only) ──
-- Roles: user_text, user_asr_final, user_transcript_edit,
--        app_question, user_answer
-- Bank data is NOT a turn.
CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id           TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
    seq               INTEGER NOT NULL,          -- 1-based, gapless per conversation
    role              TEXT NOT NULL CHECK (role IN (
        'user_text', 'user_asr_final', 'user_transcript_edit',
        'app_question', 'user_answer'
    )),
    text              TEXT NOT NULL,
    edits_turn_id     TEXT,          -- for user_transcript_edit: the original turn being corrected
    answers_question_id TEXT,         -- for user_answer: the app_question turn being answered
    created_at        TEXT NOT NULL,
    UNIQUE (conversation_id, seq),
    FOREIGN KEY (edits_turn_id) REFERENCES conversation_turns(turn_id),
    FOREIGN KEY (answers_question_id) REFERENCES conversation_turns(turn_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_conversation ON conversation_turns(conversation_id);

-- ── Audio evidence (gateway-only storage) ──
CREATE TABLE IF NOT EXISTS audio_evidence (
    audio_id        TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_id         TEXT REFERENCES conversation_turns(turn_id),
    file_path       TEXT NOT NULL,
    sha256_digest   TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    duration_ms     INTEGER,           -- parsed from WAV header if available
    consent         INTEGER NOT NULL DEFAULT 0 CHECK (consent IN (0, 1)),
    retention_until TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audio_conversation ON audio_evidence(conversation_id);

-- ── Audit events (hash-chained) ──
-- seq is INTEGER PRIMARY AUTOINCREMENT but we enforce gapless via
-- append_audit reading the tail under the caller's BEGIN IMMEDIATE lock.
CREATE TABLE IF NOT EXISTS audit_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('gateway', 'agent', 'validator')),
    session_id     TEXT,
    conversation_id TEXT,
    user_id        TEXT,
    step           TEXT NOT NULL,
    payload_json   TEXT NOT NULL,       -- canonical JSON
    trace_id       TEXT,
    span_id        TEXT,
    prev_hash      TEXT NOT NULL,       -- 64 hex chars; genesis = 64 zeros
    row_hash       TEXT NOT NULL        -- 64 hex chars
);

CREATE INDEX IF NOT EXISTS idx_audit_conversation ON audit_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_audit_source ON audit_events(source);

-- ════════════════════════════════════════════════════════════════
-- T-05: Identity, WebAuthn, frozen drafts, authorizations
-- ════════════════════════════════════════════════════════════════

-- ── Enrollment codes (bootstrap, single-use, 15-minute expiry) ──
CREATE TABLE IF NOT EXISTS enrollment_codes (
    code_hash    TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    created_at   TEXT NOT NULL
);

-- ── WebAuthn credentials ──
CREATE TABLE IF NOT EXISTS webauthn_credentials (
    credential_id   TEXT PRIMARY KEY,   -- base64url
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    public_key      BLOB NOT NULL,       -- CBOR-encoded COSE key
    sign_count      INTEGER NOT NULL DEFAULT 0,
    transports      TEXT,                -- comma-separated
    backup_eligible INTEGER NOT NULL DEFAULT 0,
    backup_state    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cred_user ON webauthn_credentials(user_id);

-- ── Server-side sessions (random 256-bit id, stored hashed) ──
CREATE TABLE IF NOT EXISTS user_sessions (
    session_hash         TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    created_at           TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    last_activity        TEXT NOT NULL,
    idle_timeout_seconds INTEGER NOT NULL DEFAULT 1800
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);

-- ── Auth challenges (server-side stored, short expiry, single use) ──
CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_hash TEXT PRIMARY KEY,
    user_id        TEXT,
    purpose        TEXT NOT NULL,   -- 'registration' | 'authentication'
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    consumed_at    TEXT
);

-- ── Frozen drafts (immutable) ──
CREATE TABLE IF NOT EXISTS frozen_drafts (
    bundle_id              TEXT PRIMARY KEY,
    conversation_id        TEXT NOT NULL,
    conversation_revision  INTEGER NOT NULL,
    owner_user_id          TEXT NOT NULL REFERENCES users(user_id),
    canonical_bytes        BLOB NOT NULL,
    bundle_hash            TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'DRAFT_FROZEN',
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_conv ON frozen_drafts(conversation_id);

-- ── Authorizations (signing envelopes) ──
CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id       TEXT PRIMARY KEY,
    bundle_id              TEXT NOT NULL,
    user_id                TEXT NOT NULL REFERENCES users(user_id),
    credential_id          TEXT NOT NULL,
    operation              TEXT NOT NULL,
    envelope_bytes         BLOB NOT NULL,
    envelope_hash          TEXT NOT NULL,
    active_mandate_version INTEGER NOT NULL,
    conversation_id       TEXT,
    conversation_revision  INTEGER,
    status                 TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
    issued_at              TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_user ON authorizations(user_id);

-- ── Approval evidence ──
CREATE TABLE IF NOT EXISTS approval_evidence (
    authorization_id        TEXT PRIMARY KEY REFERENCES authorizations(authorization_id),
    credential_id           TEXT NOT NULL,
    sign_count              INTEGER NOT NULL,
    client_data_hash        TEXT NOT NULL,
    authenticator_data_hash TEXT NOT NULL,
    verified_at             TEXT NOT NULL
);
"""


# ════════════════════════════════════════════════════════════════
# T-06 (v4): execution, receipts and idempotency
# ════════════════════════════════════════════════════════════════

#: Columns added to ``transactions`` so every leg effect is traceable to its
#: authorization.  ``direction`` separates the debit of a leg from the credit
#: an own-account transfer makes to its destination; spending ceilings and
#: velocity count debits only.
_TRANSACTION_COLUMNS_V4: dict[str, str] = {
    "authorization_id": "TEXT",
    "bundle_id": "TEXT",
    "leg_id": "TEXT",
    "leg_index": "INTEGER",
    "direction": "TEXT NOT NULL DEFAULT 'debit' CHECK (direction IN ('debit', 'credit'))",
    "quantity": "INTEGER",
    "quote_id": "TEXT",
    "settlement_ref": "TEXT",
}

#: A NULL ``expires_at`` means the mandate does not expire.
_MANDATE_COLUMNS_V4: dict[str, str] = {
    "expires_at": "TEXT",
    "max_legs": "INTEGER NOT NULL DEFAULT 4 CHECK (max_legs BETWEEN 1 AND 4)",
}

#: Comma-separated stable reason codes for the latest status change.
_AUTHORIZATION_COLUMNS_V4: dict[str, str] = {
    "status_reason": "TEXT",
}

#: One receipt per committed operation.  ``UNIQUE (user_id, operation,
#: operation_id)`` is the idempotency key; ``payload_hash`` is the immutable
#: hash of the frozen payload that key committed.
_RECEIPTS_V4_DDL = """
CREATE TABLE receipts (
    receipt_id       TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL UNIQUE REFERENCES authorizations(authorization_id),
    user_id          TEXT NOT NULL REFERENCES users(user_id),
    operation        TEXT NOT NULL,
    operation_id     TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    envelope_hash    TEXT NOT NULL,
    receipt_json     TEXT NOT NULL,   -- canonical JSON
    created_at       TEXT NOT NULL,
    UNIQUE (user_id, operation, operation_id)
)
"""

_INDEXES_V4 = (
    "CREATE INDEX IF NOT EXISTS idx_receipts_user ON receipts(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_user_time "
    "ON transactions(user_id, direction, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_authz ON transactions(authorization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_holdings_owner_symbol "
    "ON holdings(owner_user_id, symbol)",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Apply the T-06 changes.  Idempotent: every step checks current shape.

    The v3 ``receipts`` table referenced a single transaction and was never
    written.  It is replaced by the v4 shape; if a database somehow holds v3
    receipt rows they are kept as ``receipts_legacy_v3`` rather than dropped.
    """
    _add_missing_columns(conn, "transactions", _TRANSACTION_COLUMNS_V4)
    _add_missing_columns(conn, "mandates", _MANDATE_COLUMNS_V4)
    _add_missing_columns(conn, "authorizations", _AUTHORIZATION_COLUMNS_V4)

    if "authorization_id" not in _columns(conn, "receipts"):
        conn.execute("DROP INDEX IF EXISTS idx_receipts_user")
        if conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]:
            conn.execute("ALTER TABLE receipts RENAME TO receipts_legacy_v3")
        else:
            conn.execute("DROP TABLE receipts")
        conn.execute(_RECEIPTS_V4_DDL)

    for statement in _INDEXES_V4:
        conn.execute(statement)


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it does not exist and migrate to the current version.

    Idempotent: calling twice produces the same result.  The v3 baseline is
    created with ``CREATE ... IF NOT EXISTS``; later versions are applied by
    shape-checking migration functions inside one ``BEGIN IMMEDIATE``
    transaction, so concurrent initializers cannot both alter a table.
    """
    conn.executescript(_SCHEMA_DDL)

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        current = int(row[0]) if row is not None else 0
        if current < 4:
            _migrate_to_v4(conn)
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif current < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'version'",
                (str(SCHEMA_VERSION),),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _apply_feature_ddl(conn)


#: Additive DDL owned by feature modules (one file each, e.g. ``validation.sql``).
#: Only ``CREATE TABLE/INDEX IF NOT EXISTS`` belongs here; changes to existing tables
#: go in a versioned migration above.
FEATURE_DDL_DIR = Path(__file__).with_name("ddl")


def _apply_feature_ddl(conn: sqlite3.Connection) -> None:
    for path in sorted(FEATURE_DDL_DIR.glob("*.sql")):
        conn.executescript(path.read_text(encoding="utf-8"))
