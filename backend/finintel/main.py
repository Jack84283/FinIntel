"""Local-only backend API for the FinIntel workstation."""

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from finintel import __version__

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
