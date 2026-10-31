"""Durable daily acquisition monitoring and local controls."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from finintel.market_data.daily_acquisition_store import (
    get_active_job,
    get_job,
    get_latest_job,
    get_settings,
    list_history,
    provider_usage_status,
)
from finintel.market_data.daily_scheduler import DailyAcquisitionScheduler, next_scheduled_execution
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/daily-acquisition", tags=["daily-acquisition"])
_SCHEDULER = DailyAcquisitionScheduler()


class DailySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    schedule_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    primary_provider: Literal["alpaca_sip", "tiingo", "alpha_vantage"] | None = None
    secondary_provider: Literal["alpaca_sip", "tiingo", "alpha_vantage"] | None = None
    overlap_days: int | None = Field(default=None, ge=0, le=60)
    retry_attempts: int | None = Field(default=None, ge=1, le=5)
    retry_backoff_seconds: float | None = Field(default=None, ge=0, le=300)
    catch_up_limit: int | None = Field(default=None, ge=1, le=30)
    provider_limits: dict[
        Literal["alpaca_sip", "tiingo", "alpha_vantage"],
        dict[str, int],
    ] | None = None


def scheduler() -> DailyAcquisitionScheduler:
    return _SCHEDULER


def start_scheduler() -> None:
    _SCHEDULER.start()


def stop_scheduler() -> None:
    _SCHEDULER.stop()


def _require_local_write(request: Request) -> None:
    token = os.getenv("FININTEL_PROCESS_TOKEN", "")
    if token:
        if not compare_digest(request.headers.get("x-finintel-process-token", ""), token):
            raise HTTPException(status_code=403, detail="Managed desktop required")
        return
    if request.headers.get("x-finintel-client") != "local-workstation":
        raise HTTPException(status_code=403, detail="Local workstation request required")


def _latest_successful() -> dict[str, object] | None:
    result = list_history(status="completed", limit=1)
    items = result.get("items", [])
    return items[0] if items else None


def _status_payload() -> dict[str, object]:
    settings = get_settings()
    active = get_active_job()
    latest = active or get_latest_job()
    successful = _latest_successful()
    if latest is None:
        status = "idle"
        current: dict[str, object] = {
            "id": None,
            "kind": None,
            "provider": settings["primary_provider"],
            "secondary_provider": settings["secondary_provider"],
            "scheduled_for": None,
            "started_at": None,
            "finished_at": None,
            "current_instrument": None,
            "total_instruments": 0,
            "completed_instruments": 0,
            "failed_instruments": 0,
            "incomplete_instruments": 0,
            "pending_instruments": 0,
            "successful_imports": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "retry_count": 0,
            "provider_errors": [],
            "index_results": {},
            "duration_ms": None,
            "items": [],
        }
    else:
        current = latest
        status = str(latest["status"])
        if status == "scheduled":
            status = "scheduled"
        elif status == "running":
            status = "running"
        elif status == "paused":
            status = "paused"
        elif status not in ("completed", "partial", "failed", "cancelled"):
            status = "failed"
        detailed = get_job(str(latest["id"]))
        if detailed:
            current = detailed
    items = current.get("items", [])
    last_updated = max(
        (str(item["last_session"]) for item in items if item.get("last_session")),
        default=None,
    )
    last_successful_session = max(
        (
            str(item["last_session"])
            for item in items
            if item.get("status") == "completed" and item.get("last_session")
        ),
        default=None,
    )
    return {
        "status": status,
        "enabled": bool(settings["enabled"]),
        "scheduled_execution_time": settings["schedule_time"],
        "timezone": settings["timezone"],
        "primary_provider": settings["primary_provider"],
        "secondary_provider": settings["secondary_provider"],
        "catch_up_limit": settings["catch_up_limit"],
        "last_execution_time": current.get("finished_at") or current.get("started_at"),
        "last_successful_execution": successful.get("finished_at") if successful else None,
        "next_scheduled_execution": next_scheduled_execution(
            datetime.now(UTC),
            str(settings["schedule_time"]),
            str(settings["timezone"]),
        ).isoformat(timespec="seconds"),
        "current_instrument": current.get("current_instrument"),
        "total_instruments": current.get("total_instruments", 0),
        "completed_instruments": current.get("completed_instruments", 0),
        "failed_instruments": current.get("failed_instruments", 0),
        "incomplete_instruments": current.get("incomplete_instruments", 0),
        "pending_instruments": current.get("pending_instruments", 0),
        "successful_imports": current.get("successful_imports", 0),
        "inserted": current.get("inserted", 0),
        "updated": current.get("updated", 0),
        "unchanged": current.get("unchanged", 0),
        "retry_count": current.get("retry_count", 0),
        "job_duration_ms": current.get("duration_ms"),
        "last_updated_trading_session": last_updated,
        "last_successful_trading_session": last_successful_session,
        "provider_quota": provider_usage_status(),
        "provider_errors": current.get("provider_errors", []),
        "index_results": current.get("index_results", {}),
        "job_id": current.get("id"),
        "job": current,
    }


@router.get("/status")
def daily_acquisition_status() -> dict[str, object]:
    try:
        return _status_payload()
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/settings")
def daily_acquisition_settings() -> dict[str, object]:
    try:
        return get_settings()
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/settings")
def save_daily_acquisition_settings(
    payload: DailySettingsRequest, request: Request
) -> dict[str, object]:
    _require_local_write(request)
    values = payload.model_dump(exclude_unset=True)
    try:
        return _SCHEDULER.update_settings(values)
    except (StorageError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/run")
def run_daily_acquisition(request: Request) -> dict[str, object]:
    _require_local_write(request)
    try:
        return _SCHEDULER.trigger_manual()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (StorageError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _control(job_id: str, action: str, request: Request) -> dict[str, object]:
    _require_local_write(request)
    try:
        result = {
            "pause": _SCHEDULER.pause,
            "resume": _SCHEDULER.resume,
            "cancel": _SCHEDULER.cancel,
            "retry-failed": _SCHEDULER.retry_failed,
        }[action](job_id)
    except (StorageError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Daily acquisition job not found")
    return result


@router.post("/jobs/{job_id}/pause")
def pause_daily_job(job_id: str, request: Request) -> dict[str, object]:
    return _control(job_id, "pause", request)


@router.post("/jobs/{job_id}/resume")
def resume_daily_job(job_id: str, request: Request) -> dict[str, object]:
    return _control(job_id, "resume", request)


@router.post("/jobs/{job_id}/cancel")
def cancel_daily_job(job_id: str, request: Request) -> dict[str, object]:
    return _control(job_id, "cancel", request)


@router.post("/jobs/{job_id}/retry-failed")
def retry_daily_job(job_id: str, request: Request) -> dict[str, object]:
    return _control(job_id, "retry-failed", request)


@router.get("/jobs/{job_id}")
def get_daily_job(job_id: str) -> dict[str, object]:
    result = get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Daily acquisition job not found")
    return result


@router.get("/history")
def daily_acquisition_history(
    q: str = Query(default="", max_length=80),
    provider: str = Query(default="", max_length=40),
    status: str = Query(default="", max_length=20),
    kind: str = Query(default="", max_length=20),
    execution_date: str = Query(default="", max_length=10),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    try:
        return list_history(
            query=q,
            provider=provider,
            status=status,
            kind=kind,
            execution_date=execution_date,
            limit=limit,
            offset=offset,
        )
    except (StorageError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
