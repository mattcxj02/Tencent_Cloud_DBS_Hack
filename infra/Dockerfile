# DCTA service images — T-04 runtime isolation
#
# One Dockerfile with a SERVICE build arg producing three images:
#   gateway   (UID 10001) — contains gateway/ ledger/ shared/
#   agent     (UID 10002) — contains agent/ shared/ prompts/
#   validator (UID 10003) — contains validator/ shared/ prompts/
#
# Agent and validator images contain NO gateway/ or ledger/ source.
# Each runs as a distinct non-root user; application code is owned by root.
#
# Build:
#   docker build --build-arg SERVICE=gateway   -t dcta-gateway   -f infra/Dockerfile .
#   docker build --build-arg SERVICE=agent     -t dcta-agent     -f infra/Dockerfile .
#   docker build --build-arg SERVICE=validator -t dcta-validator -f infra/Dockerfile .

ARG SERVICE=gateway

# ── Builder stage: install all dependencies from the lockfile ──────────
# The venv is created at /app/.venv so shebangs match the runtime path.
FROM python:3.12-slim AS builder

# git is required because trtc-asr is a git source in uv.lock
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install uv (pinned to match the project)
COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /uvx /bin/

# Create /app so the venv lives at /app/.venv (matching runtime)
RUN mkdir -p /app
WORKDIR /app

# Copy only lockfile + project metadata for dependency resolution
COPY pyproject.toml uv.lock ./

# Sync dependencies from the lockfile (frozen, no dev, no project install)
# Creates /app/.venv with all runtime dependencies including trtc-asr.
# UV_PROJECT_ENVIRONMENT defaults to .venv under WORKDIR → /app/.venv
RUN uv sync --frozen --no-dev --no-install-project

# ── Gateway runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS gateway

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

RUN groupadd -g 10001 dcta && \
    useradd -u 10001 -g dcta -d /app -s /sbin/nologin dcta && \
    mkdir -p /app /data/audio && \
    chown -R dcta:dcta /data

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=root:root --chmod=0555 shared/ /app/shared/
COPY --chown=root:root --chmod=0555 gateway/ /app/gateway/
COPY --chown=root:root --chmod=0555 ledger/ /app/ledger/

RUN touch /app/shared/__init__.py /app/gateway/__init__.py /app/ledger/__init__.py

USER dcta

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8002"]

# ── Agent runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim AS agent

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

RUN groupadd -g 10002 dcta && \
    useradd -u 10002 -g dcta -d /app -s /sbin/nologin dcta

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=root:root --chmod=0555 shared/ /app/shared/
COPY --chown=root:root --chmod=0555 agent/ /app/agent/
COPY --chown=root:root --chmod=0555 prompts/ /app/prompts/

RUN touch /app/shared/__init__.py /app/agent/__init__.py

USER dcta

CMD ["uvicorn", "agent.app:app", "--host", "0.0.0.0", "--port", "8001"]

# ── Validator runtime ──────────────────────────────────────────────────
FROM python:3.12-slim AS validator

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

RUN groupadd -g 10003 dcta && \
    useradd -u 10003 -g dcta -d /app -s /sbin/nologin dcta

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=root:root --chmod=0555 shared/ /app/shared/
COPY --chown=root:root --chmod=0555 validator/ /app/validator/
COPY --chown=root:root --chmod=0555 prompts/ /app/prompts/

RUN touch /app/shared/__init__.py /app/validator/__init__.py

USER dcta

CMD ["uvicorn", "validator.app:app", "--host", "0.0.0.0", "--port", "8003"]
