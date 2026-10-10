-- T-07: signed mandate changes (operation update_mandate).
--
-- Every committed change is recorded here with the full target settings it signed.
-- A tightening (decrease, removed action, pause) is APPLIED at once as a new
-- `mandates` version.  A loosening (increase, added action, unpause) waits as the
-- user's single PENDING change until effective_at (cooling), then becomes ACTIVATED
-- with a new `mandates` version; until then the old version stays in force.  A new
-- loosening or any tightening CANCELS the pending one.  Money is INTEGER cents.
CREATE TABLE IF NOT EXISTS mandate_changes (
    change_id         TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    authorization_id  TEXT NOT NULL UNIQUE REFERENCES authorizations(authorization_id),
    direction         TEXT NOT NULL CHECK (direction IN ('tighten', 'loosen')),
    base_version      INTEGER NOT NULL,
    per_leg_cents     INTEGER NOT NULL CHECK (per_leg_cents > 0),
    daily_cents       INTEGER NOT NULL CHECK (daily_cents > 0),
    allowed_actions   TEXT NOT NULL,  -- comma-separated Action values
    paused            INTEGER NOT NULL CHECK (paused IN (0, 1)),
    status            TEXT NOT NULL CHECK (status IN ('APPLIED', 'PENDING', 'ACTIVATED', 'CANCELLED')),
    status_reason     TEXT,
    effective_at      TEXT NOT NULL,
    resulting_version INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- At most one pending (cooling) change per user.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mandate_changes_one_pending
    ON mandate_changes(user_id) WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_mandate_changes_user ON mandate_changes(user_id, created_at);
