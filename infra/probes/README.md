# infra/probes

`isolation_probe.py` (stdlib only) runs inside the agent and validator containers and records PASS/FAIL for each check: no ledger file or `*.db`, `gateway`/`ledger` not importable, read-only code and root filesystem, no Docker socket or host workspace, no gateway secrets in the environment (names only), 401 for missing/malformed/forged tokens and fabricated user turns, no route to the other model-facing service, expected non-root UID.

Run with `make isolation-probe`; output goes to `docs/evidence/T-04-isolation.txt`, and the target fails if any check fails.
