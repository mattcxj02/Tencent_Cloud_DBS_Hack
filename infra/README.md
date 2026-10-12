# infra

Container images and Compose configuration that make the trust boundary real (invariant I1).

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build with three targets. Each image contains only its own code: gateway → `gateway/ ledger/ shared/`; agent → `agent/ shared/ prompts/`; validator → `validator/ shared/ prompts/`. Distinct non-root users (10001/10002/10003); code owned by root |
| `compose.yaml` | Gateway publishes 8002 on localhost only and alone mounts the `gateway_data` volume. Agent and validator: no ports, no volumes, read-only root, `cap_drop: ALL`, `no-new-privileges`, and separate networks (`agent_net`, `validator_net`) so they cannot reach each other. Each service receives only its own secrets |
| `probes/` | In-container isolation probes (see `probes/README.md`) |

```bash
make compose-build      # build images
make compose-up         # start the stack
make isolation-probe    # build, start, probe both containers, prove no .env in images, tear down
make compose-down
```

`.dockerignore` keeps `.env`, `data/`, `docs/` and `references/` out of every build context. Limitations: all containers share one Docker host and kernel, user namespaces are not remapped, and `make dev` (local processes) does not provide this isolation.

Compose forwards extractor `EXTRACT_TIMEOUT_SECONDS`/`EXTRACT_MAX_RETRIES`, gateway `DRAFT_TIMEOUT_SECONDS` (overall budget, default 90 s)/`AGENT_TIMEOUT_SECONDS`/`VALIDATOR_CLIENT_TIMEOUT_SECONDS` and validator `VALIDATE_TIMEOUT_SECONDS`. The shared validator token still goes only to gateway and validator and must match (≥32 characters). Health checks use liveness to avoid startup dependency cycles; verify drafting with gateway `/ready` after the stack starts. Recreate the relevant containers after environment changes.

Run one gateway process: Phase 4 drafting admission and run revocation are in memory; multiple workers/replicas require shared coordination.
