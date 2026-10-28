"""Read-only index level APIs and an explicitly authorized FRED acquisition."""

import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from finintel.market_data.index_history import (
    IndexHistoryError,
    acquire_index_closes,
    index_catalog,
    read_index_closes,
)
from finintel.settings.api import _require_managed
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/market-data/index-history", tags=["index-history"])

IndexCode = Literal["SP500", "DJIA", "NASDAQCOM", "NDX", "VIX"]
_FETCH_LOCK = threading.Lock()


class IndexCatalogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: IndexCode
    name: str
    fred_series: str
    provider: Literal["fred"]
    source_url: str
    sessions: int
    first_session: str | None
    last_session: str | None
    data_kind: Literal["daily_close_only"]


class IndexClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: str
    close: str


class IndexHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: IndexCode
    name: str
    data_kind: Literal["daily_close_only"]
    unit: Literal["index_points"]
    provider: Literal["fred"]
    source_url: str
    has_more: bool
    observations: list[IndexClose]


@router.get("", response_model=list[IndexCatalogRecord])
def get_index_history_catalog() -> list[dict[str, object]]:
    try:
        return index_catalog()
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{code}/daily", response_model=IndexHistoryResponse)
def get_index_daily_closes(
    code: IndexCode, limit: int = Query(default=4000, ge=1, le=10000)
) -> dict[str, object]:
    try:
        return read_index_closes(code, limit=limit)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{code}/acquire")
def download_index_daily_closes(code: IndexCode, request: Request) -> dict[str, object]:
    """Never call FRED on page load; only explicit trusted native desktop action."""
    _require_managed(request)
    if not _FETCH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Index acquisition already running")
    try:
        try:
            return acquire_index_closes(code)
        except IndexHistoryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        _FETCH_LOCK.release()
