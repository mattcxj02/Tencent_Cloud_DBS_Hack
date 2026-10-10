-- T-07: simulated out-of-band step-up (secondary approval) challenges.
--
-- One challenge per authorization, bound to the user, the authorization and its
-- envelope hash.  The six-digit code is NEVER stored: only a per-challenge salt and
-- a keyed verifier (HMAC-SHA256 with a gateway-held secret) are kept, so the table
-- alone does not allow an offline search of the 10^6 code space.
--
-- status: PENDING (usable) -> CONSUMED (valid code accepted inside the commit
-- transaction) | BLOCKED (5 failed attempts or binding mismatch) | EXPIRED |
-- SUPERSEDED (correction / mandate change) | CANCELLED | INVALIDATED (the
-- authorization left AWAITING_STEP_UP some other way, or the commit failed after a
-- valid code).  'PENDING' + conversation columns let a conversation correction
-- supersede the challenge through gateway.audit.REVISION_BOUND_TABLES.
CREATE TABLE IF NOT EXISTS step_up_challenges (
    challenge_id          TEXT PRIMARY KEY,
    authorization_id      TEXT NOT NULL UNIQUE REFERENCES authorizations(authorization_id),
    user_id               TEXT NOT NULL,
    envelope_hash         TEXT NOT NULL,
    conversation_id       TEXT,
    conversation_revision INTEGER,
    salt                  TEXT NOT NULL,
    verifier              TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'CONSUMED', 'BLOCKED', 'EXPIRED', 'SUPERSEDED', 'CANCELLED', 'INVALIDATED'
    )),
    status_reason         TEXT,
    failed_attempts       INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    max_attempts          INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    deliveries            INTEGER NOT NULL DEFAULT 0,
    issued_at             TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    last_delivered_at     TEXT,
    closed_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_step_up_user_status ON step_up_challenges(user_id, status);
