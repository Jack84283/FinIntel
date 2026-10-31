"""Read-only index level APIs and an explicitly authorized FRED acquisition."""

import threading
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from finintel.market_data.daily_acquisition_store import reset_provider_status
from finintel.market_data.index_history import (
    IndexHistoryError,
    acquire_index_closes,
    index_catalog,
    read_index_closes,
)
from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderRateLimited,
    ProviderUnavailable,
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
    series_type: Literal["price", "volatility"]
    frequency: Literal["daily"]
    currency: str | None
    units: Literal["index_points"]
    licensing_note: str
    known_history_limit: str
    sessions: int
    first_session: str | None
    last_session: str | None
    coverage_updated_at: str | None
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
    code: IndexCode,
    limit: int = Query(default=4000, ge=1, le=10000),
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> dict[str, object]:
    try:
        return read_index_closes(code, limit=limit, start=start, end=end)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{code}/acquire")
def download_index_daily_closes(
    code: IndexCode,
    request: Request,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> dict[str, object]:
    """Never call FRED on page load; only explicit trusted native desktop action."""
    _require_managed(request)
    if not _FETCH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Index acquisition already running")
    try:
        try:
            # This endpoint is an explicit individual download. Reset only the
            # persisted FRED failure marker; request counts and budgets remain.
            reset_provider_status("fred")
            if start is None and end is None:
                return acquire_index_closes(code)
            return acquire_index_closes(code, start=start, end=end)
        except ProviderRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (ProviderAccessDenied, ProviderUnavailable) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except IndexHistoryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        _FETCH_LOCK.release()
