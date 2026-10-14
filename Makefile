# DCTA Makefile — common development targets
.PHONY: env eval-fake operator reset-db upgrade-db test lint web-build dev stop seed eval smoke-llm smoke-asr smoke-langfuse smoke-validate smoke-telemetry smoke-extract smoke-voice \
       compose-build compose-up compose-down isolation-probe enroll-code

# ── Setup ──

# Create .env from .env.example, generate service secrets, ask for API keys.
# Safe to rerun: existing values are kept.
env:
	python3 scripts/setup_env.py

# ── Development checks ──

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check .

web-build:
	cd web && npm install && npm run build

# ── Enrollment code generation ──

# One-time WebAuthn enrollment code (15 min). Make's own $(USER) is your login name,
# so the demo user is passed as ENROLL_USER.
ENROLL_USER ?= user-matthew
enroll-code:
	uv run python -m gateway.cli_enroll USER=$(ENROLL_USER)

# ── Service startup ──

dev:
	uv run python scripts/dev.py

stop:
	uv run python scripts/dev.py stop

# ── Data and evaluations ──

seed:
	uv run python -m ledger.cli

# Bring an existing database up to date (new tables, new seed aliases) and keep
# enrolled passkeys, conversations and history. Safe to run more than once.
upgrade-db:
	uv run python -m ledger.cli --upgrade

# Delete the local database and start fresh (needed once passkeys/authorizations exist,
# because the seed cannot delete users they reference).
reset-db:
	rm -f $${DCTA_DB_PATH:-data/dcta.db} $${DCTA_DB_PATH:-data/dcta.db}-wal $${DCTA_DB_PATH:-data/dcta.db}-shm
	$(MAKE) seed

eval:
	uv run python -m evals.run

eval-fake:
	uv run python -m evals.run --fake

# Simulated phone for step-up codes: make operator ARGS='list' or ARGS='code <authz_id>'
# Uses an exported OPERATOR_TOKEN if set, otherwise only that one value from .env.
operator:
	@OPERATOR_TOKEN="$${OPERATOR_TOKEN:-$$(sed -n 's/^OPERATOR_TOKEN=//p' .env 2>/dev/null)}" uv run python scripts/operator_console.py $(ARGS)

# ── Smoke checks ──

smoke-llm:
	uv run python scripts/smoke_llm.py

smoke-asr:
	uv run python scripts/smoke_asr.py

smoke-langfuse:
	uv run python scripts/smoke_langfuse.py

smoke-validate:
	uv run python scripts/smoke_validate.py

smoke-telemetry:
	uv run python scripts/smoke_telemetry.py

smoke-extract:
	uv run python scripts/smoke_extract.py

smoke-voice:
	uv run python scripts/smoke_voice.py

# ── Compose (T-04 runtime isolation) ──
COMPOSE := docker compose -f infra/compose.yaml --project-directory .

compose-build:
	$(COMPOSE) build

compose-up:
	$(COMPOSE) up -d

compose-down:
	$(COMPOSE) down --volumes

# Isolation probe: bring stack up, run probes in agent+validator, tear down,
# write combined output to docs/evidence/T-04-isolation.txt.
isolation-probe:
	@mkdir -p docs/evidence
	@echo "=== T-04 Isolation Probe ===" > docs/evidence/T-04-isolation.txt
	@echo "Started: $$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> docs/evidence/T-04-isolation.txt
	@echo "" >> docs/evidence/T-04-isolation.txt
	@echo "Building images…" >&2
	@$(COMPOSE) build 2>&1 | tail -5
	@echo "Starting stack…" >&2
	@$(COMPOSE) up -d 2>&1 | tail -5
	@echo "Waiting for health…" >&2
	@$(COMPOSE) up -d --wait --wait-timeout 120 2>&1 | tail -3
	@echo "" >&2
	@echo "Running probe in agent container…" >&2
	@echo "--- AGENT PROBE ---" >> docs/evidence/T-04-isolation.txt
	@$(COMPOSE) exec -T -e SERVICE=agent -e GATEWAY_HOST=gateway agent sh -c 'cat > /tmp/probe.py && python /tmp/probe.py' < infra/probes/isolation_probe.py >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "" >> docs/evidence/T-04-isolation.txt
	@echo "Running probe in validator container…" >&2
	@echo "--- VALIDATOR PROBE ---" >> docs/evidence/T-04-isolation.txt
	@$(COMPOSE) exec -T -e SERVICE=validator -e GATEWAY_HOST=gateway validator sh -c 'cat > /tmp/probe.py && python /tmp/probe.py' < infra/probes/isolation_probe.py >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "" >> docs/evidence/T-04-isolation.txt
	@echo "Running .env-absent proof…" >&2
	@echo "--- .ENV ABSENT PROOF ---" >> docs/evidence/T-04-isolation.txt
	@echo "agent image:" >> docs/evidence/T-04-isolation.txt
	@docker run --rm --entrypoint sh dcta-agent:latest -c 'find / -name ".env" -not -path "/proc/*" 2>/dev/null' >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "(expected: empty)" >> docs/evidence/T-04-isolation.txt
	@echo "validator image:" >> docs/evidence/T-04-isolation.txt
	@docker run --rm --entrypoint sh dcta-validator:latest -c 'find / -name ".env" -not -path "/proc/*" 2>/dev/null' >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "(expected: empty)" >> docs/evidence/T-04-isolation.txt
	@echo "gateway image:" >> docs/evidence/T-04-isolation.txt
	@docker run --rm --entrypoint sh dcta-gateway:latest -c 'find / -name ".env" -not -path "/proc/*" 2>/dev/null' >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "(expected: empty)" >> docs/evidence/T-04-isolation.txt
	@echo "" >> docs/evidence/T-04-isolation.txt
	@echo "Image sizes:" >> docs/evidence/T-04-isolation.txt
	@docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | grep dcta >> docs/evidence/T-04-isolation.txt 2>&1 || true
	@echo "" >> docs/evidence/T-04-isolation.txt
	@echo "Finished: $$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> docs/evidence/T-04-isolation.txt
	@echo "Tearing down…" >&2
	@$(COMPOSE) down --volumes 2>&1 | tail -3
	@echo "Results written to docs/evidence/T-04-isolation.txt"
	@cat docs/evidence/T-04-isolation.txt
	@# Fail the target unless both probes report 0 FAIL and every image lacks .env.
	@test "$$(grep -c 'SUMMARY: .* 0 FAIL' docs/evidence/T-04-isolation.txt)" = "2" || { echo "ISOLATION PROBE FAILED" >&2; exit 1; }
	@! grep -q '/\.env$$' docs/evidence/T-04-isolation.txt || { echo ".env FOUND IN AN IMAGE" >&2; exit 1; }
	@echo "isolation-probe: all checks passed" >&2
