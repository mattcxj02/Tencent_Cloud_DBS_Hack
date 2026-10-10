-- Semantic validation results (T-10). Owned by gateway/validation.py.
--
-- One row per validation attempt of a frozen draft. A row is bound to the exact
-- bundle (id + hash), the conversation (id, revision, hash), the bank labels shown to
-- the validator (bank_metadata_hash) and the request it checked (input_hash). The
-- verdict comes only from the validator response; no route accepts a verdict.
--
-- state: PENDING (call in flight), CURRENT (the latest finished result for the
-- bundle; at most one), SUPERSEDED (an older attempt, or bound to an old revision).
-- status/verdict: OK + MATCH | MISMATCH | UNCERTAIN, or ERROR + code. Only a
-- CURRENT, OK, MATCH row can make a draft eligible for envelope issuance.
CREATE TABLE IF NOT EXISTS validation_results (
    result_id             TEXT PRIMARY KEY,
    bundle_id             TEXT NOT NULL,
    owner_user_id         TEXT NOT NULL,
    bundle_hash           TEXT NOT NULL,
    conversation_id       TEXT NOT NULL,
    conversation_revision INTEGER NOT NULL,
    conversation_hash     TEXT NOT NULL,
    bank_metadata_hash    TEXT NOT NULL,
    input_hash            TEXT NOT NULL,
    request_json          TEXT NOT NULL,
    attempt               INTEGER NOT NULL CHECK (attempt >= 1),
    prompt_hash           TEXT,
    model                 TEXT,
    status                TEXT CHECK (status IS NULL OR status IN ('OK', 'ERROR')),
    verdict               TEXT CHECK (verdict IS NULL OR verdict IN ('MATCH', 'MISMATCH', 'UNCERTAIN')),
    issues_json           TEXT,
    code                  TEXT,
    latency_ms            INTEGER,
    usage_json            TEXT,
    trace_id              TEXT,
    span_id               TEXT,
    state                 TEXT NOT NULL DEFAULT 'PENDING'
                          CHECK (state IN ('PENDING', 'CURRENT', 'SUPERSEDED')),
    created_at            TEXT NOT NULL,
    completed_at          TEXT,
    UNIQUE (bundle_id, attempt),
    CHECK (
        (status IS 'OK' AND verdict IS NOT NULL AND code IS NULL)
        OR (status IS 'ERROR' AND verdict IS NULL AND code IS NOT NULL)
        OR (status IS NULL AND verdict IS NULL)
    ),
    CHECK (state IS NOT 'CURRENT' OR status IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_validation_bundle
    ON validation_results(bundle_id, attempt);
CREATE INDEX IF NOT EXISTS idx_validation_conversation
    ON validation_results(conversation_id, conversation_revision);
CREATE INDEX IF NOT EXISTS idx_validation_user_time
    ON validation_results(owner_user_id, created_at);
-- At most one finished CURRENT result and one in-flight attempt per bundle.
CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_current
    ON validation_results(bundle_id) WHERE state = 'CURRENT';
CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_in_flight
    ON validation_results(bundle_id) WHERE state = 'PENDING';
