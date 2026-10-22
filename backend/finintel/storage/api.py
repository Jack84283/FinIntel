"""Read-only historical market data API for the local development backend."""

from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from finintel.storage.db import StorageError, list_imports, list_instruments, read_daily

router = APIRouter(prefix="/api/v1/market-data", tags=["historical-market-data"])


class InstrumentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    provider: str
    frequency: str
    adjustment: str
    data_kind: str
    sessions: int
    first_session: str | None
    last_session: str | None


class DailyBarRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_date: date
    open: str
    high: str
    low: str
    close: str
    volume: int = Field(ge=0)


class DailySeriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    provider: str
    frequency: str
    price_adjustment: str
    data_kind: str
    source_url: str | None
    provider_terms_url: str | None
    retrieved_at: str | None
    provider_last_refreshed: str | None
    has_more: bool
    observations: list[DailyBarRecord]


class IngestionHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    symbol: str
    provider: str
    retrieved_at: str
    imported_at: str
    payload_sha256: str
    first_session: str
    last_session: str
    observations: int
    inserted: int
    updated: int
    unchanged: int
    stale: int
    source_url: str


def _unavailable(exc: StorageError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/instruments", response_model=list[InstrumentRecord])
def get_instruments() -> list[dict[str, object]]:
    """No database yet => [] (no implicit migrations or writes)."""
    try:
        return list_instruments()
    except StorageError as exc:
        raise _unavailable(exc) from exc


@router.get("/imports", response_model=list[IngestionHistoryRecord])
def get_imports(limit: int = Query(default=25, ge=1, le=100)) -> list[dict[str, object]]:
    try:
        return list_imports(limit=limit)
    except StorageError as exc:
        raise _unavailable(exc) from exc


@router.get("/{symbol}/daily", response_model=DailySeriesResponse)
def get_daily(
    symbol: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,14}$"),
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=250, ge=1, le=1000),
    latest: bool = False,
) -> dict[str, object]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must not exceed end_date")
    try:
        result = read_daily(
            symbol.upper(), start=start_date, end=end_date, limit=limit, latest=latest
        )
    except StorageError as exc:
        raise _unavailable(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="No stored data for this symbol")
    return result
