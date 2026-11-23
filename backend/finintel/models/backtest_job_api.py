"""Fixed managed transport for durable SMA backtests."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from finintel.models.analysis_job_api import (
    JobStatus,
    _authorize,
    _job_id,
    _read_job_body,
    _request_errors,
    _storage_errors,
)
from finintel.models.backtest_worker import preflight_backtest
from finintel.models.durable_backtest import DurableBacktestRequest
from finintel.models.job_store import MAX_ATTEMPTS, ModelJobLedger

BACKTEST_JOBS_PATH = "/api/v1/models/backtests/jobs"
router = APIRouter(
    prefix=BACKTEST_JOBS_PATH,
    tags=["local-backtest-jobs"],
    dependencies=[Depends(_authorize)],
)


def _queue(payload: bytearray) -> dict:
    ledger = ModelJobLedger()
    with _storage_errors(), _request_errors():
        request = DurableBacktestRequest.from_wire(payload)
        preflight_backtest(ledger, request)
        return ledger.enqueue_backtest(request)


@router.post("", status_code=202)
async def create_backtest_job(request: Request) -> dict:
    return await run_in_threadpool(_queue, await _read_job_body(request))


@router.get("")
def list_backtest_jobs(
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    status: JobStatus | None = None,
) -> dict:
    with _storage_errors():
        return ModelJobLedger().list(limit=limit, offset=offset, status=status, backtest_only=True)


@router.get("/{job_id}")
def get_backtest_job(job_id: str, include_result: bool = False) -> dict:
    with _storage_errors():
        item = ModelJobLedger().get(
            _job_id(job_id), include_result=include_result, backtest_only=True
        )
        if item is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return item


@router.post("/{job_id}/cancel")
def cancel_backtest_job(job_id: str) -> dict:
    with _storage_errors():
        state = ModelJobLedger().cancel(_job_id(job_id), backtest_only=True)
        if state is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return {"state": state}


@router.post("/{job_id}/retry")
def retry_backtest_job(job_id: str) -> dict:
    identifier = _job_id(job_id)
    ledger = ModelJobLedger()
    with _storage_errors():
        existing = ledger.get(identifier, backtest_only=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        if existing["state"] not in {"failed", "cancelled"} or existing["attempts"] >= MAX_ATTEMPTS:
            raise HTTPException(status_code=409, detail="job_conflict")
        original = DurableBacktestRequest.model_validate_json(json.dumps(existing["request"]))
        with _request_errors():
            preflight_backtest(ledger, original)
        if not ledger.retry(identifier, backtest_only=True, expected_attempt=existing["attempts"]):
            raise HTTPException(status_code=409, detail="job_conflict")
        item = ledger.get(identifier, backtest_only=True)
        if item is None:
            raise HTTPException(status_code=503, detail="storage_unavailable")
        return item
