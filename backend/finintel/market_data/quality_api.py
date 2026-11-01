"""HTTP API for durable, reviewable local data-quality findings."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from finintel.market_data.quality import (
    FINDING_TYPES,
    REVIEW_STATES,
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.settings.api import _require_managed
from finintel.storage.db import database_path

router = APIRouter(prefix="/api/v1/quality", tags=["data-quality"])


class AnomalyThresholdRequest(BaseModel):
    price_jump_ratio: Decimal | None = Field(default=None, gt=1)
    volume_spike_ratio: Decimal | None = Field(default=None, gt=1)
    volume_spike_minimum: int | None = Field(default=None, ge=1)
    volume_baseline_minimum: int | None = Field(default=None, ge=1)
    zero_volume_price_move_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    zero_volume_baseline_minimum: int | None = Field(default=None, ge=1)
    stale_after_days: int | None = Field(default=None, ge=1)
    volume_lookback: int | None = Field(default=None, ge=2)


class ScanRequest(BaseModel):
    max_findings: int = Field(default=1000, ge=1, le=10_000)
    thresholds: AnomalyThresholdRequest | None = None


class ReviewRequest(BaseModel):
    state: Literal["candidate", "confirmed", "rejected", "accepted_correction"]
    note: str | None = Field(default=None, max_length=2000)


@router.post("/scan")
def scan(request: ScanRequest, http_request: Request) -> dict:
    _require_managed(http_request)
    try:
        thresholds = (
            request.thresholds.model_dump(exclude_none=True)
            if request.thresholds is not None
            else None
        )
        return run_quality_scan(
            database_path(), max_findings=request.max_findings, thresholds=thresholds
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/findings")
def findings(
    finding_type: str | None = Query(default=None),
    code: str | None = Query(default=None, max_length=32),
    provider: str | None = Query(default=None, max_length=80),
    review_state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if finding_type is not None and finding_type not in FINDING_TYPES:
        raise HTTPException(status_code=422, detail="Unknown finding type.")
    if review_state is not None and review_state not in REVIEW_STATES:
        raise HTTPException(status_code=422, detail="Unknown review state.")
    try:
        return list_findings(
            database_path(),
            finding_type=finding_type,
            code=code,
            provider=provider,
            review_state=review_state,
            limit=limit,
            offset=offset,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc


@router.post("/findings/{finding_id}/review")
def review(finding_id: int, request: ReviewRequest, http_request: Request) -> dict:
    _require_managed(http_request)
    try:
        return review_finding(database_path(), finding_id, request.state, request.note)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Quality finding was not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/report")
def report(limit: int = Query(default=500, ge=1, le=10_000)) -> dict:
    try:
        return quality_report(database_path(), limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc
