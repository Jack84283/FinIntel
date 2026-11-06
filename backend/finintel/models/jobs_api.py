"""Managed desktop-only, fixed-route durable research job API (Issue #58)."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from finintel.models.job_store import ModelJobLedger, ModelJobRequest, ModelJobStoreError
from finintel.settings.api import _require_managed

router = APIRouter(prefix="/api/v1/models/jobs", tags=["local-research-model-jobs"])


def _ledger_error(exc: ModelJobStoreError) -> HTTPException:
    # Only these fixed messages may cross the API; never expose raw SQLite paths,
    # provider credentials, request JSON or model exception contents.
    if str(exc) == "Model-job queue is full":
        return HTTPException(status_code=429, detail="Local model-job queue is full")
    if "storage" in str(exc).lower() or "integrity" in str(exc).lower():
        return HTTPException(status_code=503, detail="Local model-job storage unavailable")
    return HTTPException(status_code=422, detail="Invalid local model-job operation")


def _get(identifier: UUID, *, include_result: bool = False) -> dict:
    try:
        record = ModelJobLedger().get(str(identifier), include_result=include_result)
    except ModelJobStoreError as exc:
        raise _ledger_error(exc) from None
    if record is None:
        raise HTTPException(status_code=404, detail="Local model job not found")
    return record


@router.post("", status_code=201)
def enqueue_job(payload: ModelJobRequest, request: Request) -> dict:
    _require_managed(request)
    try:
        return ModelJobLedger().enqueue(payload)
    except ModelJobStoreError as exc:
        raise _ledger_error(exc) from None


@router.get("")
def list_jobs(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> dict:
    _require_managed(request)
    try:
        return ModelJobLedger().list(limit=limit, offset=offset)
    except ModelJobStoreError as exc:
        raise _ledger_error(exc) from None


@router.get("/{identifier}")
def get_job(identifier: UUID, request: Request) -> dict:
    _require_managed(request)
    return _get(identifier)


@router.get("/{identifier}/result")
def get_job_result(identifier: UUID, request: Request) -> dict:
    _require_managed(request)
    record = _get(identifier, include_result=True)
    if record["state"] != "completed":
        raise HTTPException(status_code=409, detail="Local model job has no completed result")
    return record


@router.post("/{identifier}/cancel")
def cancel_job(identifier: UUID, request: Request) -> dict:
    _require_managed(request)
    try:
        state = ModelJobLedger().cancel(str(identifier))
    except ModelJobStoreError as exc:
        raise _ledger_error(exc) from None
    if state is None:
        raise HTTPException(status_code=404, detail="Local model job not found")
    return {"id": str(identifier), "status": state}


@router.post("/{identifier}/retry")
def retry_job(identifier: UUID, request: Request) -> dict:
    _require_managed(request)
    ledger = ModelJobLedger()
    try:
        record = ledger.get(str(identifier))
        if record is None:
            raise HTTPException(status_code=404, detail="Local model job not found")
        # A repeated retry of an already-queued job is harmless and idempotent.
        if record["state"] != "queued" and not ledger.retry(str(identifier)):
            raise HTTPException(status_code=409, detail="Local model job cannot be retried")
        return _get(identifier)
    except ModelJobStoreError as exc:
        raise _ledger_error(exc) from None
