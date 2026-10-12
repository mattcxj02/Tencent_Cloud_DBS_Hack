CREATE TABLE IF NOT EXISTS draft_calculations (
    bundle_id TEXT PRIMARY KEY REFERENCES frozen_drafts(bundle_id),
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollment_challenges (
    challenge_hash TEXT PRIMARY KEY REFERENCES auth_challenges(challenge_hash),
    code_hash TEXT NOT NULL REFERENCES enrollment_codes(code_hash)
);
CREATE INDEX IF NOT EXISTS idx_enrollment_challenges_code ON enrollment_challenges(code_hash);
