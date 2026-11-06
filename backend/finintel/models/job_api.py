"""Managed-token-only, fixed-route durable model-job API (Issue #58)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from finintel.market_data.analytics import AnalyticsError
from finintel.models.api import ModelRunInput
from finintel.models.job_store import ModelJobLedger, ModelJobRequest, ModelJobStoreError
from finintel.models.job_worker import source_snapshot_for_job
from finintel.settings.api import _require_managed
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/models/jobs", tags=["local-research-jobs"])


def _valid_job_id(identifier: str) -> str:
    try:
        if str(uuid.UUID(identifier)) == identifier:
            return identifier
    except (ValueError, AttributeError):
        pass
    raise HTTPException(status_code=422, detail="Invalid local model-job identifier")


def _storage_failure(exc: ModelJobStoreError) -> HTTPException:
    message = str(exc)
    if message in {"Model-job queue is full", "Invalid model-job pagination"}:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=503, detail="Local model-job storage is unavailable")


@router.post("", status_code=202)
def enqueue_model_job(payload: ModelRunInput, request: Request) -> dict:
    """Pin the *local* source before durable queuing; no model evaluation here."""
    _require_managed(request)
    try:
        snapshot = source_snapshot_for_job(
            code=payload.code,
            start=payload.start,
            end=payload.end,
            quality_policy=payload.quality_policy,
        )
    except (AnalyticsError, StorageError) as exc:
        raise HTTPException(
            status_code=422, detail="Local model source is unavailable or fails the quality policy"
        ) from exc
    if payload.expected_snapshot_sha256 is not None and (
        payload.expected_snapshot_sha256 != snapshot
    ):
        raise HTTPException(status_code=409, detail="Local model source revision changed")
    try:
        typed = ModelJobRequest.model_validate(
            {**payload.model_dump(), "expected_snapshot_sha256": snapshot}
        )
        return ModelJobLedger().enqueue(typed)
    except ModelJobStoreError as exc:
        raise _storage_failure(exc) from exc


@router.get("")
def list_model_jobs(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> dict:
    _require_managed(request)
    try:
        return ModelJobLedger().list(limit=limit, offset=offset)
    except ModelJobStoreError as exc:
        raise _storage_failure(exc) from exc


@router.get("/{job_id}")
def get_model_job(job_id: str, request: Request, include_result: bool = False) -> dict:
    _require_managed(request)
    _valid_job_id(job_id)
    try:
        item = ModelJobLedger().get(job_id, include_result=include_result)
    except ModelJobStoreError as exc:
        raise _storage_failure(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Model job not found")
    return item


@router.post("/{job_id}/cancel")
def cancel_model_job(job_id: str, request: Request) -> dict:
    _require_managed(request)
    _valid_job_id(job_id)
    try:
        state = ModelJobLedger().cancel(job_id)
    except ModelJobStoreError as exc:
        raise _storage_failure(exc) from exc
    if state is None:
        raise HTTPException(status_code=404, detail="Model job not found")
    return {"state": state}


@router.post("/{job_id}/retry")
def retry_model_job(job_id: str, request: Request) -> dict:
    _require_managed(request)
    _valid_job_id(job_id)
    ledger = ModelJobLedger()
    try:
        existing = ledger.get(job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Model job not found")
        if not ledger.retry(job_id):
            raise HTTPException(
                status_code=409, detail="Model job cannot be retried in its current state"
            )
        item = ledger.get(job_id)
        assert item is not None
        return item
    except ModelJobStoreError as exc:
        raise _storage_failure(exc) from exc
