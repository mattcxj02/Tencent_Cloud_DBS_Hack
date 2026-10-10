"""Gateway service FastAPI application.

The gateway owns identity, conversation store, frozen drafts, validation
coordination, policy/mandates, approval/step-up, execution, and audit.
It is the only service with ledger/evidence mount and identity/secrets.
The browser talks only to the gateway; agent and validator are internal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.bank_routes import router as bank_router
from gateway.conv_routes import router as conv_router
from gateway.draft_routes import router as draft_router
from gateway.identity_routes import router as identity_router
from gateway.mandate_routes import router as mandate_router
from gateway.operator_routes import router as operator_router
from gateway.stepup_routes import router as stepup_router
from gateway.validation import get_validation_telemetry
from gateway.validation_routes import router as validation_router
from shared.settings import load_gateway_settings


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    # Flush the last telemetry batch; never let shutdown fail on telemetry.
    with suppress(Exception):
        get_validation_telemetry().shutdown()


app = FastAPI(title="DCTA Gateway", docs_url="/docs", redoc_url=None, lifespan=_lifespan)

# CORS restricted to the configured WebAuthn RP origin.
_settings = load_gateway_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.rp_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

app.include_router(bank_router)
app.include_router(conv_router)
app.include_router(identity_router)
app.include_router(draft_router)
app.include_router(validation_router)
app.include_router(stepup_router)
app.include_router(mandate_router)
app.include_router(operator_router)


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, str]:
    """Health check — reports credential status without exposing secrets."""
    settings = load_gateway_settings()
    return {
        "service": "gateway",
        "status": "ok",
        "trtc": "configured" if settings.trtc_enabled else "missing-credentials",
        "langfuse": "configured" if settings.langfuse_enabled else "missing-credentials",
    }
