"""Local historical market data reads and explicitly authorized acquisition."""

import threading
from datetime import date
from time import monotonic
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from finintel.market_data.acquisition import acquire_and_store, key_configured
from finintel.market_data.alpaca import alpaca_configured
from finintel.market_data.provider import (
    SYMBOL_PATTERN,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderUnavailable,
)
from finintel.market_data.symbol_catalog import fetch_active_listings
from finintel.market_data.tiingo import tiingo_configured
from finintel.storage.db import (
    StorageError,
    append_action_event,
    list_action_events,
    list_imports,
    list_instruments,
    lookup_symbol_type,
    read_daily,
    replace_symbol_catalog,
    search_symbols,
)

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
    limit: int = Query(default=250, ge=1, le=4000),
    latest: bool = False,
    provider: Literal["alpha_vantage", "alpaca_sip", "tiingo"] = "alpha_vantage",
) -> dict[str, object]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must not exceed end_date")
    try:
        result = read_daily(
            symbol.upper(),
            start=start_date,
            end=end_date,
            limit=limit,
            latest=latest,
            provider=provider,
        )
    except StorageError as exc:
        raise _unavailable(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="No stored data for this symbol")
    return result


# This is a loopback *development* service, not the authenticated tablet gateway.
# Cross-origin browser forms are denied: mutations require a custom JSON header
# and allowlisted Origin. Tauri uses its fixed native IPC command to supply
# the header; the React/Vite development proxy uses same-origin requests.
_ACQUIRE_LOCK = threading.Lock()
_ALLOWED_ORIGINS = {"http://127.0.0.1:1420", "http://localhost:1420"}


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str
    asset_type: Literal["Stock", "ETF", "Index", "Mutual Fund"]
    exchange: str


class SymbolSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["starter", "alpha_vantage"]
    updated_at: str | None
    total: int
    results: list[CatalogEntry]


class CatalogSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["synced"]
    total: int
    updated_at: str
    source: Literal["alpha_vantage"]


@router.get("/symbols", response_model=SymbolSearchResponse)
def get_symbols(
    q: str = Query(default="", max_length=80),
    asset_type: Literal["Stock", "ETF", "Index", "Mutual Fund"] | None = None,
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, object]:
    try:
        return search_symbols(query=q, asset_type=asset_type, limit=limit)
    except StorageError as exc:
        raise _unavailable(exc) from exc


class AcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=15)
    outputsize: Literal["compact", "full"] = "compact"
    provider: Literal["alpha_vantage", "alpaca_sip", "tiingo"] = "alpha_vantage"
    period: Literal["5y", "10y", "max"] = "10y"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(ticker):
            raise ValueError("Invalid equity ticker symbol")
        return ticker


class AcquisitionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["alpha_vantage"] = "alpha_vantage"
    key_configured: bool
    requires_explicit_action: bool = True
    api_key_environment_variable: str = "FININTEL_ALPHA_VANTAGE_API_KEY"
    full_requires_premium: bool = True
    tiingo_configured: bool = False
    tiingo_key_environment_variable: str = "FININTEL_TIINGO_API_KEY"
    alpaca_configured: bool = False
    alpaca_key_environment_variable: str = "FININTEL_ALPACA_API_KEY_ID"
    alpaca_secret_environment_variable: str = "FININTEL_ALPACA_API_SECRET_KEY"


class AcquisitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["stored"]
    symbol: str
    provider: str
    frequency: str
    price_adjustment: str
    first_session: str
    last_session: str
    retrieved_at: str
    observations: int
    inserted: int
    updated: int
    unchanged: int
    stale: int
    import_id: int
    not_live_quotes: bool


@router.get("/acquisition/status", response_model=AcquisitionStatus)
def acquisition_status() -> AcquisitionStatus:
    return AcquisitionStatus(
        key_configured=key_configured(),
        alpaca_configured=alpaca_configured(),
        tiingo_configured=tiingo_configured(),
    )


def _acquire_market_data_impl(payload: AcquisitionRequest, request: Request) -> dict[str, object]:
    """One explicit user request. No scheduled fetches and no input API keys."""
    _authorize_local_write(request)
    asset_type = lookup_symbol_type(payload.symbol)
    if asset_type in ("Index", "Mutual Fund"):
        raise HTTPException(
            status_code=422,
            detail=f"{asset_type} history is not supported by the current stock/ETF price provider",
        )
    if payload.provider == "alpha_vantage" and not key_configured():
        raise HTTPException(
            status_code=503,
            detail="Configure FININTEL_ALPHA_VANTAGE_API_KEY and restart the backend",
        )
    if payload.provider == "alpaca_sip" and not alpaca_configured():
        raise HTTPException(
            status_code=503,
            detail="Configure FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY",
        )
    if not _ACQUIRE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An acquisition is already in progress")
    try:
        try:
            return acquire_and_store(
                payload.symbol,
                outputsize=payload.outputsize,
                provider=payload.provider,
                period=payload.period,
            )
        except ProviderRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ProviderUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ProviderPayloadInvalid as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (StorageError, OSError) as exc:
            raise HTTPException(status_code=503, detail="Local SQLite import failed") from exc
    finally:
        _ACQUIRE_LOCK.release()


class ActionEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    occurred_at: str
    action: Literal["acquisition"]
    status: Literal["success", "failed"]
    symbol: str
    provider: str
    import_id: int | None
    error_code: str | None
    duration_ms: int = Field(ge=0)


@router.get("/action-events", response_model=list[ActionEventRecord])
def get_action_events(limit: int = Query(default=100, ge=1, le=100)) -> list[dict[str, object]]:
    try:
        return list_action_events(limit=limit)
    except StorageError as exc:
        raise _unavailable(exc) from exc


@router.post("/acquisition", response_model=AcquisitionResult)
def acquire_market_data(payload: AcquisitionRequest, request: Request) -> dict[str, object]:
    """Record authorized acquisition outcomes without persisting secrets or exception text."""
    _authorize_local_write(request)
    started = monotonic()
    result: dict[str, object] | None = None
    error_code: str | None = None
    try:
        result = _acquire_market_data_impl(payload, request)
        return result
    except HTTPException as exc:
        error_code = f"http_{exc.status_code}"
        raise
    except Exception:
        error_code = "internal_error"
        raise
    finally:
        try:
            append_action_event(
                symbol=payload.symbol,
                provider=payload.provider,
                status="success" if result is not None else "failed",
                import_id=int(result["import_id"]) if result is not None else None,
                error_code=error_code,
                duration_ms=max(0, int((monotonic() - started) * 1000)),
            )
        except (StorageError, OSError):
            # Never turn a completed import into an apparent failed import.
            # An unavailable database cannot provide durable audit guarantees.
            pass


@router.post("/symbols/sync", response_model=CatalogSyncResult)
def sync_symbols(request: Request) -> dict[str, object]:
    """Explicit provider listing refresh; SQLite change is all-or-nothing."""
    _authorize_local_write(request, require_json=False)
    if not key_configured():
        raise HTTPException(
            status_code=503,
            detail="Configure FININTEL_ALPHA_VANTAGE_API_KEY and restart the Python backend",
        )
    if not _ACQUIRE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another acquisition is in progress")
    try:
        try:
            rows = fetch_active_listings()
            return replace_symbol_catalog(rows)
        except ProviderRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (ProviderUnavailable, ProviderPayloadInvalid) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (StorageError, OSError) as exc:
            raise HTTPException(
                status_code=503, detail="Unable to update local symbol catalog"
            ) from exc
    finally:
        _ACQUIRE_LOCK.release()


def _authorize_local_write(request: Request, *, require_json: bool = True) -> None:
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1", "testclient"}
        or request.headers.get("x-finintel-client") != "local-workstation"
        or request.headers.get("origin") not in (None, *_ALLOWED_ORIGINS)
        or (
            require_json
            and request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        )
    ):
        raise HTTPException(status_code=403, detail="Local workstation request required")
