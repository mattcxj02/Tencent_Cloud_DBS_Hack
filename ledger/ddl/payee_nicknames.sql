-- Customer-chosen nicknames for their own saved payees ("mom" -> Mum).
-- Aliases (payee_aliases) are set by the bank; nicknames are added and removed by the
-- customer on the Payees page. Both are exact names for matching and evidence.
-- name_key is the nickname with case, spaces and punctuation removed
-- (shared.names.compact), so "Ah Boy" and "ahboy" cannot both exist for one customer.
CREATE TABLE IF NOT EXISTS payee_nicknames (
    owner_user_id TEXT NOT NULL REFERENCES users(user_id),
    name_key      TEXT NOT NULL,
    nickname      TEXT NOT NULL,
    payee_id      TEXT NOT NULL REFERENCES payees(payee_id),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, name_key)
);
CREATE INDEX IF NOT EXISTS idx_payee_nicknames_payee ON payee_nicknames (payee_id);
