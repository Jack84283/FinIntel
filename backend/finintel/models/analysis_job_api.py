"""Authenticated transport for approved Analysis Library jobs."""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from finintel.market_data.analytics import AnalyticsError
from finintel.models.contracts import AnalysisRequest, Revision
from finintel.models.job_store import MAX_ATTEMPTS, ModelJobLedger, ModelJobStoreError
from finintel.models.job_worker import _safe_error_code, enqueue_analysis_job
from finintel.models.managed_analysis import preflight_analysis_job
from finintel.models.managed_approvals import MANAGED_ANALYSIS_REVISIONS
from finintel.settings.api import _require_managed
from finintel.storage.db import StorageError

ANALYSIS_JOBS_PATH = "/api/v1/models/analysis/jobs"


def _authorize(request: Request) -> None:
    try:
        _require_managed(request)
    except (HTTPException, TypeError) as exc:
        raise HTTPException(status_code=403, detail="managed_required") from exc


router = APIRouter(
    prefix=ANALYSIS_JOBS_PATH,
    tags=["local-analysis-jobs"],
    dependencies=[Depends(_authorize)],
)
JobStatus = Literal["queued", "running", "cancel_requested", "completed", "failed", "cancelled"]


class CreateAnalysisJob(AnalysisRequest):
    """Add the caller's expected revision to the existing pinned request."""

    implementation_revision: Revision


def _job_id(identifier: str) -> str:
    try:
        if str(uuid.UUID(identifier)) == identifier:
            return identifier
    except ValueError:
        pass
    raise HTTPException(status_code=422, detail="invalid_input")


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except ModelJobStoreError as exc:
        if str(exc) == "Model-job queue is full":
            raise HTTPException(status_code=409, detail="queue_full") from exc
        raise HTTPException(status_code=503, detail="storage_unavailable") from exc
    except (StorageError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="storage_unavailable") from exc


@contextmanager
def _request_errors() -> Iterator[None]:
    try:
        yield
    except ModelJobStoreError:
        raise
    except AnalyticsError as exc:
        code = _safe_error_code(exc)
        raise HTTPException(
            status_code=409 if code == "source_changed" else 422, detail=code
        ) from exc
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="invalid_input") from exc


@router.post("", status_code=202)
async def create_analysis_job(request: Request) -> dict:
    return await run_in_threadpool(_queue, await _read_job_body(request))


async def _read_job_body(request: Request) -> bytearray:
    # Route dependencies authenticate before the bounded body is read.
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > 8192:
            raise HTTPException(status_code=422, detail="invalid_input")
        payload.extend(chunk)
    return payload


def _queue(payload: bytearray) -> dict:
    with _storage_errors(), _request_errors():
        typed = CreateAnalysisJob.model_validate_json(payload)
        revision = MANAGED_ANALYSIS_REVISIONS.get((typed.model_id, typed.model_version))
        if revision != typed.implementation_revision:
            raise HTTPException(status_code=422, detail="invalid_input")
        request = AnalysisRequest.model_validate_json(
            typed.model_dump_json(exclude={"implementation_revision"})
        )
        return enqueue_analysis_job(request)


@router.get("")
def list_analysis_jobs(
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    status: JobStatus | None = None,
) -> dict:
    with _storage_errors():
        return ModelJobLedger().list(limit=limit, offset=offset, status=status, analysis_only=True)


@router.get("/{job_id}")
def get_analysis_job(job_id: str, include_result: bool = False) -> dict:
    with _storage_errors():
        item = ModelJobLedger().get(
            _job_id(job_id), include_result=include_result, analysis_only=True
        )
        if item is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return item


@router.post("/{job_id}/cancel")
def cancel_analysis_job(job_id: str) -> dict:
    with _storage_errors():
        state = ModelJobLedger().cancel(_job_id(job_id), analysis_only=True)
        if state is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return {"state": state}


@router.post("/{job_id}/retry")
def retry_analysis_job(job_id: str) -> dict:
    identifier = _job_id(job_id)
    ledger = ModelJobLedger()
    with _storage_errors():
        existing = ledger.get(identifier, analysis_only=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        if existing["state"] not in {"failed", "cancelled"} or existing["attempts"] >= MAX_ATTEMPTS:
            raise HTTPException(status_code=409, detail="job_conflict")
        request = AnalysisRequest.model_validate_json(json.dumps(existing["request"]))
        with _request_errors():
            preflight_analysis_job(request)
        if not ledger.retry(identifier, analysis_only=True, expected_attempt=existing["attempts"]):
            raise HTTPException(status_code=409, detail="job_conflict")
        item = ledger.get(identifier, analysis_only=True)
        if item is None:
            raise HTTPException(status_code=503, detail="storage_unavailable")
        return item
