"""Managed-desktop local-only versioned research model API."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from finintel.market_data.analytics import AnalyticsError
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.registry import MODEL_ID, MODEL_VERSION, ModelParameters, model_registry
from finintel.settings.api import _require_managed
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/models", tags=["local-research-models"])


class ModelRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: Literal["builtin.sma_crossover"] = MODEL_ID
    model_version: Literal["1.0.0"] = MODEL_VERSION
    code: Literal["SP500", "DJIA", "NASDAQCOM", "NDX"] = "SP500"
    start: date
    end: date
    quality_policy: Literal["require_complete", "allow_unverified"] = "require_complete"
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    expected_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ModelResearchPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_date: str
    close: str
    fast_sma: str | None
    slow_sma: str | None
    signal_after_close: Literal["long", "flat"] | None


class ModelResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_contract: Literal["model-research-evaluation-v1"]
    lineage: dict
    reproducibility_sha256: str
    output_sha256: str
    evaluated_at_utc: str
    status: Literal["completed"]
    observations: int
    points: list[ModelResearchPoint]
    quality_warning: str | None
    read_only: bool
    no_network: bool
    no_synthetic_sessions: bool
    signal_after_close_only: bool
    backtest_validated: bool
    persistent_job: bool


@router.get("/registry")
def get_model_registry() -> dict:
    return model_registry()


@router.post("/evaluate", response_model=ModelResearchResult)
def evaluate_model(payload: ModelRunInput, request: Request) -> dict:
    # This explicit execution endpoint remains desktop-only even in developer mode.
    # No arbitrary code, Python paths or plugin identifiers can reach execution.
    _require_managed(request)
    try:
        return evaluate_builtin_indicator(
            model_id=payload.model_id,
            model_version=payload.model_version,
            code=payload.code,
            start=payload.start,
            end=payload.end,
            parameters=payload.parameters,
            quality_policy=payload.quality_policy,
            expected_snapshot_sha256=payload.expected_snapshot_sha256,
        )
    except AnalyticsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
