# gateway/approval

Eligibility hooks for the approval and execution pipeline. Routes and `gateway/drafts.py` call these hooks; all defaults are the real implementations.

| File | Purpose |
|---|---|
| `hooks.py` | `PolicyOutcome` (decision + stable `reason_codes`) and `Receipt` types; `semantic_validation_passed` (→ `gateway.validation.semantic_validation_passed`: True only for a CURRENT, OK `MATCH` bound to the exact bundle ID/hash, owner, conversation revision/hash and bank labels while that revision is current; any error is False), `policy_precheck` (→ `gateway.policy.evaluate` at the gateway clock), `commit_authorization` (→ `gateway.execution.commit`). Test overrides via `_set_hook` — not reachable from HTTP |

There is no runtime bypass flag, header, or environment variable. Tests use the module-level registry, `gateway.clock._set_clock` or `app.dependency_overrides`.

Overriding `policy_precheck` only affects the pre-checks at issuance and right after the signature. `gateway.execution.commit` always re-runs the real policy under the write lock, so an override cannot make a blocked payment move money.
