"""Local-only backend API for the FinIntel workstation."""

import os
from hmac import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from finintel import __version__
from finintel.market_data.daily_status_api import router as daily_status_router
from finintel.market_data.index_history_api import router as index_history_router
from finintel.market_data.membership_api import router as membership_router
from finintel.market_data.validation_api import router as validation_router
from finintel.settings.api import router as settings_router
from finintel.storage.api import router as market_data_router

app = FastAPI(
    title="FinIntel Local API",
    version=__version__,
    description="Local development backend. No external network access is required.",
    docs_url=None,
    redoc_url=None,
)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    service: str
    version: str


@app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="finintel-backend", version=__version__)


app.include_router(market_data_router)
app.include_router(validation_router)
app.include_router(membership_router)
app.include_router(index_history_router)
app.include_router(daily_status_router)
app.include_router(settings_router)


@app.middleware("http")
async def require_desktop_process_token(request: Request, call_next):
    """An app-owned sidecar rejects requests from unrelated loopback clients."""
    expected = os.getenv("FININTEL_PROCESS_TOKEN", "")
    if expected and not compare_digest(
        request.headers.get("x-finintel-process-token", ""), expected
    ):
        return JSONResponse(
            status_code=403, content={"detail": "Desktop process authorization required"}
        )
    return await call_next(request)
