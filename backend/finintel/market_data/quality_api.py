"""HTTP API for durable, reviewable local data-quality findings."""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from finintel.market_data.corrections import (
    CorrectionConflict,
    get_correction,
    list_corporate_actions,
    list_corrections,
    preview_correction,
    propose_correction,
    record_verified_actions,
    resolved_close,
    transition_correction,
)
from finintel.market_data.provider import ProviderError
from finintel.market_data.quality import (
    FINDING_TYPES,
    REVIEW_STATES,
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.market_data.reconciliation import ReconciliationPolicy, reconciliation_summary
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
    reconciliation_relative_tolerance: Decimal | None = Field(default=None, ge=0, le=1)
    reconciliation_absolute_tolerance: Decimal | None = Field(default=None, ge=0)
    reconciliation_provider_priority: (
        list[Literal["alpaca_sip", "tiingo", "alpha_vantage"]] | None
    ) = None


class ScanRequest(BaseModel):
    max_findings: int = Field(default=1000, ge=1, le=10_000)
    thresholds: AnomalyThresholdRequest | None = None
    as_of: date | None = None


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
            database_path(),
            max_findings=request.max_findings,
            thresholds=thresholds,
            as_of=request.as_of,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/reconciliation/policy")
def reconciliation_policy() -> dict:
    """Read-only default policy and explicit source eligibility."""
    policy = ReconciliationPolicy()
    return {
        **policy.identity(),
        "note": "Provider priority is advisory; no winning observation is selected.",
    }


@router.get("/reconciliation/summary")
def reconciliation_status(code: str | None = Query(default=None, max_length=32)) -> dict:
    """Read-only aggregate; no external provider requests or local mutations."""
    try:
        return reconciliation_summary(database_path(), code=code)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc


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
def report(
    limit: int = Query(default=500, ge=1, le=10_000),
    as_of: date | None = None,
) -> dict:
    try:
        return quality_report(database_path(), limit=limit, as_of=as_of)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="Local market database is unavailable."
        ) from exc


class CorrectionProposalRequest(BaseModel):
    finding_id: int = Field(gt=0)
    provider: str = Field(min_length=1, max_length=80)
    proposed_close: Decimal = Field(gt=0)
    rationale: str = Field(min_length=1, max_length=2000)


class CorrectionTransitionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class CorporateReferenceRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)


def _correction_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc.args[0]))
    if isinstance(exc, CorrectionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=503, detail="Local market database is unavailable.")
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/corporate-actions/verify")
def verify_corporate_actions(payload: CorporateReferenceRequest, request: Request) -> dict:
    """Explicit provider request; never initiated automatically during scanning."""
    _require_managed(request)
    try:
        return record_verified_actions(database_path(), payload.symbol)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        raise _correction_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/corporate-actions")
def corporate_actions(
    code: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    try:
        return list_corporate_actions(database_path(), code, limit=limit)
    except (FileNotFoundError, ValueError) as exc:
        raise _correction_error(exc) from exc


@router.post("/corrections")
def propose(payload: CorrectionProposalRequest, request: Request) -> dict:
    _require_managed(request)
    try:
        return propose_correction(
            database_path(),
            finding_id=payload.finding_id,
            provider=payload.provider,
            proposed_close=str(payload.proposed_close),
            rationale=payload.rationale,
        )
    except (KeyError, ValueError, FileNotFoundError) as exc:
        raise _correction_error(exc) from exc


@router.get("/corrections")
def corrections(
    code: str | None = Query(default=None, max_length=32),
    state: Literal["proposed", "validated", "approved", "rejected", "reversed"] | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return list_corrections(database_path(), code=code, state=state, limit=limit, offset=offset)
    except (FileNotFoundError, ValueError) as exc:
        raise _correction_error(exc) from exc


@router.get("/corrections/resolve")
def resolve_correction(
    *,
    instrument_kind: Literal["equity", "index"],
    code: str = Query(min_length=1, max_length=32),
    provider: str = Query(min_length=1, max_length=80),
    adjustment: str = Query(min_length=1, max_length=80),
    frequency: str = Query(min_length=1, max_length=20),
    session_date: date,
) -> dict:
    try:
        return resolved_close(
            database_path(),
            kind=instrument_kind,
            code=code,
            provider=provider,
            adjustment=adjustment,
            frequency=frequency,
            session_date=session_date.isoformat(),
        )
    except (KeyError, ValueError, FileNotFoundError) as exc:
        raise _correction_error(exc) from exc


@router.get("/corrections/{correction_id}")
def correction(correction_id: str) -> dict:
    try:
        return get_correction(database_path(), correction_id)
    except (KeyError, FileNotFoundError) as exc:
        raise _correction_error(exc) from exc


@router.get("/corrections/{correction_id}/preview")
def preview(correction_id: str) -> dict:
    try:
        return preview_correction(database_path(), correction_id)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise _correction_error(exc) from exc


@router.post("/corrections/{correction_id}/{action}")
def decide(
    correction_id: str,
    action: Literal["validate", "approve", "reject", "reverse"],
    payload: CorrectionTransitionRequest,
    request: Request,
) -> dict:
    _require_managed(request)
    try:
        return transition_correction(database_path(), correction_id, action, payload.note)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        raise _correction_error(exc) from exc
