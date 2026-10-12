# ledger

The mock bank database: schema and seed data. **Imported only by gateway code**; the agent and validator images do not contain this package.

| File | Purpose |
|---|---|
| `schema.py` | SQLite schema and migrations (`init_db`, `connect`); money stored as INTEGER cents, never REAL; WAL, foreign keys and a 10 s busy timeout on every connection |
| `seed.py` | Idempotent synthetic seed: Matthew (savings 12,000 / current 3,500 / joint 2,000), payees incl. two Johns, Mum, Ah Ma, Landlord Lim, a sanctioned Northwind Trading, a PayNow recipient, SP Group, a card with 642.30 outstanding, SGX quotes, mandate v1 (1,000/leg, 3,000/day, 4 legs, all actions, expires 2027-12-31), and a second user (Ana) who shares the joint account |
| `cli.py` | Entry point for `make seed` (honours `DCTA_DB_PATH`) |

**Schema versions.** The v3 baseline (T-05) is created with `CREATE ... IF NOT EXISTS`; later versions are shape-checking migrations applied inside one `BEGIN IMMEDIATE` transaction, so `init_db` stays idempotent and safe to run on an existing database. v4 (T-06) adds:

- `transactions`: `authorization_id`, `bundle_id`, `leg_id`, `leg_index`, `direction` (`debit` | `credit`; an own-account transfer writes both), `quantity`, `quote_id`, `settlement_ref`; index on `(user_id, direction, created_at)` for the daily and velocity windows, which are derived from these rows.
- `receipts`: one row per committed operation, `UNIQUE (user_id, operation, operation_id)` (the idempotency key) with the immutable `payload_hash`, the envelope hash and the canonical receipt JSON. The never-written v3 receipts table is replaced (kept as `receipts_legacy_v3` if it somehow holds rows).
- `mandates`: `expires_at` (NULL = no expiry) and `max_legs` (default 4).
- `authorizations`: `status_reason` (stable reason codes of the latest status change).
- `holdings`: unique `(owner_user_id, symbol)` so equity purchases add to one position.

All data is synthetic. No real bank, PayNow or SGX connection exists.

Feature tables can also be added as idempotent SQL files in `ddl/` (see `ddl/README.md`); `init_db` applies them after migrations. T-07 adds `ddl/stepup.sql` (`step_up_challenges`: one simulated step-up challenge per authorization with salt and keyed verifier, never the code; status, failed attempts, deliveries, expiry) and `ddl/mandates.sql` (`mandate_changes`: every signed mandate change with its full target settings, `APPLIED` / `PENDING` / `ACTIVATED` / `CANCELLED`, effective time and resulting version; a partial unique index allows one `PENDING` change per user). New mandate versions are ordinary `mandates` rows; no existing table changed, so the schema version stays 4. `make reset-db` deletes the local database and re-seeds.

Phase 6 adds `draft_calculations` and `enrollment_challenges` through additive feature DDL. Run `make upgrade-db` for existing databases; it preserves credentials, codes, sessions, conversations and financial history. Old registration challenges without code bindings cannot enroll; codes already consumed under the old begin flow need a fresh operator-issued code.
