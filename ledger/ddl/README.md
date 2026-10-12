# ledger/ddl

Additive schema owned by feature modules, one `.sql` file per feature (for example `validation.sql`, `stepup.sql`). `ledger.schema.init_db` runs every file here, in name order, after the versioned migrations.

Only idempotent `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` statements belong here. Changes to existing tables go in a versioned migration in `ledger/schema.py`. Money columns are INTEGER cents, never REAL.

| File | Tables |
|---|---|
| `phase6.sql` | `draft_calculations`: immutable calculation JSON and digest per frozen bundle; `enrollment_challenges`: persistent code-to-registration-challenge binding and begin count. Additive tables installed by `make upgrade-db` |
| `validation.sql` | `validation_results`: one row per validator attempt, bound to bundle hash, conversation revision/hash, bank-label hash and input hash; `state` PENDING/CURRENT/SUPERSEDED |
| `payee_nicknames.sql` | `payee_nicknames`: customer-chosen nicknames for their own saved payees, unique per customer by `name_key` (case, spaces and punctuation removed) |
