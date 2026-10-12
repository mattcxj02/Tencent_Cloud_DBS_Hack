"""Agent service FastAPI application (internal only, port 8001).

The agent has no ledger access, no signing credentials and no execution tools. The
gateway calls ``POST /internal/run`` with ``Authorization: Bearer <delegation token>``
(bound to one user and conversation); the agent uses that same token for every call
back to the gateway, so it can only read that user's data and propose for that
conversation.

Responses (always HTTP 200 once authenticated):

- ``{"status": "clarify", "question_turn_id", "question", "reason"}``
- ``{"status": "draft_frozen", "bundle_id", "bundle_hash", "conversation_revision", "legs"}``
- ``{"status": "error", "code"}``

plus ``trace_id`` when Langfuse is configured. A valid incoming ``traceparent`` header
continues the gateway's trace, and calls back to the gateway carry it on.

A frozen draft is not validated or approved; the gateway does that independently.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.extraction import ExtractorLLM, OpenAICompatibleExtractor
from agent.gateway_client import GatewayClient
from agent.workflow import AgentDeps, run_agent
from shared.deadline import DEADLINE_HEADER, Deadline, DeadlineExpired, current_deadline
from shared.settings import load_agent_settings
from shared.telemetry import Telemetry, extract_trace_context, httpx_request_hook

logger = logging.getLogger("dcta.agent")

_telemetry: Telemetry | None = None
_telemetry_lock = threading.Lock()


def get_telemetry() -> Telemetry | None:
    """One Langfuse telemetry facade per process (disabled, not faked, without keys)."""
    global _telemetry  # noqa: PLW0603
    with _telemetry_lock:
        if _telemetry is None:
            _telemetry = Telemetry(load_agent_settings(), "agent")
        return _telemetry


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = load_agent_settings()
    with ExitStack() as stack:
        extractor = (
            OpenAICompatibleExtractor.from_settings(settings) if settings.llm_enabled else None
        )
        if extractor is not None:
            stack.callback(extractor.close)
        application.state.extractor = extractor
        application.state.gateway_http = stack.enter_context(
            httpx.Client(
                base_url=settings.gateway_base_url,
                timeout=GATEWAY_TIMEOUT_SECONDS,
                event_hooks={"request": [httpx_request_hook]},
            )
        )
        try:
            yield
        finally:
            if _telemetry is not None:
                _telemetry.shutdown()
            application.state.extractor = None


app = FastAPI(title="DCTA Agent", docs_url="/docs", redoc_url=None, lifespan=_lifespan)

GATEWAY_TIMEOUT_SECONDS = 15.0


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(..., min_length=1, max_length=128)


def get_extractor(request: Request) -> ExtractorLLM | None:
    return request.app.state.extractor


def get_gateway_http(request: Request) -> httpx.Client:
    # GatewayClient supplies the caller's delegation token on each request, never here.
    return request.app.state.gateway_http


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bearer token required"
        )
    return token.strip()


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check — reports credential status without exposing secrets."""
    settings = load_agent_settings()
    return {
        "service": "agent",
        "status": "ok",
        "llm": "configured" if settings.llm_enabled else "missing-credentials",
        "langfuse": "configured" if settings.langfuse_enabled else "missing-credentials",
    }


@app.post("/internal/run")
def run(
    body: RunRequest,
    request: Request,
    llm: Annotated[ExtractorLLM | None, Depends(get_extractor)],
    http: Annotated[httpx.Client, Depends(get_gateway_http)],
    telemetry: Annotated[Telemetry | None, Depends(get_telemetry)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Draft from the conversation's current revision: ask one question or propose."""
    token = _bearer_token(authorization)
    if llm is None:
        return {"status": "error", "code": "EXTRACTOR_UNAVAILABLE"}
    deps = AgentDeps(gateway=GatewayClient(http, token), llm=llm, telemetry=telemetry)
    budget = Deadline.from_header(
        request.headers.get(DEADLINE_HEADER), load_agent_settings().extract_timeout_seconds + 5
    )
    deadline_token = current_deadline.set(budget)
    try:
        budget.remaining()
        outcome = run_agent(
            body.conversation_id, deps, parent=extract_trace_context(request.headers)
        )
        budget.remaining()
        return outcome
    except DeadlineExpired:
        return {"status": "error", "code": "AGENT_TIMEOUT"}
    except Exception:  # noqa: BLE001 - fail closed: no proposal, no internals leaked
        logger.exception("agent run failed")
        return {"status": "error", "code": "AGENT_INTERNAL_ERROR"}
    finally:
        current_deadline.reset(deadline_token)


@app.get("/ready")
def ready() -> JSONResponse:
    """Configuration readiness only; never invokes the extractor or provider."""
    settings = load_agent_settings()
    if not settings.llm_enabled or not settings.extract_model.strip():
        return JSONResponse(
            {"status": "unavailable", "code": "EXTRACTOR_NOT_CONFIGURED"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})
