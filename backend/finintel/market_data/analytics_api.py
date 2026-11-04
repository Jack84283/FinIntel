"""Read-only local analytics API; typed, bounded, no provider requests."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_basket import run_index_basket
from finintel.market_data.analytics_store import run_analytics
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/analytics", tags=["local-analytics"])


class SeriesReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_kind: Literal["index", "equity"]
    code: str
    provider: str
    adjustment: str
    return_semantics: str
    frequency: str
    units: str


class AnalyticsPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: str
    close: str
    normalized_100: str | None
    simple_return: str | None
    log_return: str | None
    cumulative_return: str | None
    drawdown: str | None
    rolling_volatility_annualized: str | None
    moving_average: str | None
    momentum: str | None
    rsi: str | None


class ComparedPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: str
    left_normalized_100: str
    right_normalized_100: str
    left_return: str | None
    right_return: str | None
    relative_strength: str
    rolling_correlation: str | None
    rolling_covariance: str | None
    rolling_beta: str | None
    rolling_tracking_error_annualized: str | None


class DrawdownEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["recovered", "ongoing"]
    peak_session_date: str
    trough_session_date: str
    recovery_session_date: str | None
    trough_drawdown: str
    underwater_observed_sessions: int
    peak_to_recovery_observed_sessions: int | None
    trough_to_recovery_observed_sessions: int | None


class PeriodReturn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: str
    return_value: str = Field(alias="return")
    period: Literal["week", "month", "year"]


class ComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_version: str
    left: SeriesReference
    right: SeriesReference
    missing_data_policy: Literal["intersection", "strict"]
    overlap_sessions: int
    left_only_sessions: int
    right_only_sessions: int
    window_sessions: int
    annualization_sessions: int
    price_return_only: bool
    dividends_excluded: bool
    notes: list[str]
    source: dict[str, str | None]
    points: list[ComparedPoint]


class AnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_version: str
    identity: SeriesReference
    settings: dict[str, int | str | bool]
    observations: int
    first_session: str | None
    last_session: str | None
    metrics_enabled: bool
    max_drawdown: str | None
    longest_underwater_observed_sessions: int | None
    longest_completed_recovery_observed_sessions: int | None
    ongoing_drawdown_observed_sessions: int | None
    drawdown_episodes: list[DrawdownEpisode]
    period_returns: dict[str, list[PeriodReturn]]
    points: list[AnalyticsPoint]
    request: dict[str, str]
    source: dict[str, str | None]
    quality: dict[str, str | bool | int | None]
    quality_policy: Literal["require_complete", "allow_unverified"]
    unverified_data: bool
    read_only: bool
    no_network: bool
    no_synthetic_sessions: bool
    comparison: ComparisonResponse | None = None


@router.get("/series", response_model=AnalyticsResponse)
def local_analytics(
    start: Annotated[date, Query(description="Inclusive observed-history start")],
    end: Annotated[date, Query(description="Inclusive observed-history end")],
    kind: Literal["index", "equity"] = "index",
    code: str = Query(default="SP500", min_length=1, max_length=20),
    provider: str = Query(default="fred", min_length=1, max_length=40),
    window: int = Query(default=20, ge=5, le=252),
    comparison_code: str | None = Query(default=None, min_length=1, max_length=20),
    comparison_kind: Literal["index", "equity"] = "index",
    comparison_provider: str = Query(default="fred", min_length=1, max_length=40),
    missing_policy: Literal["intersection", "strict"] = "intersection",
    quality_policy: Literal["require_complete", "allow_unverified"] = "require_complete",
    allow_volatility_returns: bool = False,
) -> dict:
    try:
        return run_analytics(
            kind=kind,
            code=code,
            provider=provider,
            start=start,
            end=end,
            window=window,
            comparison_code=comparison_code,
            comparison_kind=comparison_kind,
            comparison_provider=comparison_provider,
            missing_policy=missing_policy,
            quality_policy=quality_policy,
            allow_volatility_returns=allow_volatility_returns,
        )
    except AnalyticsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class BasketMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normalized_100: str
    simple_return: str | None
    drawdown: str | None
    rolling_volatility_annualized: str | None
    relative_strength: str | None
    rolling_correlation: str | None


class BasketPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: str
    values: dict[str, BasketMetric]


class BasketSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: SeriesReference
    source: dict[str, str | None]
    stored_sessions: int
    excluded_nonshared_sessions: int


class BasketResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_version: str
    codes: list[str]
    baseline_code: str
    request: dict[str, str | int]
    quality: dict[str, str | bool | int | None]
    quality_policy: Literal["require_complete", "allow_unverified"]
    unverified_data: bool
    alignment: Literal["intersection"]
    common_sessions: int
    first_common_session: str
    last_common_session: str
    series: list[BasketSource]
    points: list[BasketPoint]
    read_only: bool
    no_network: bool
    no_synthetic_sessions: bool
    notes: list[str]


@router.get("/basket", response_model=BasketResponse)
def local_index_basket(
    start: Annotated[date, Query(description="Inclusive first requested session")],
    end: Annotated[date, Query(description="Inclusive last requested session")],
    codes: Annotated[str, Query(min_length=1, max_length=40)],
    window: int = Query(default=20, ge=5, le=252),
    quality_policy: Literal["require_complete", "allow_unverified"] = "require_complete",
) -> dict:
    # CSV contains strictly provider-independent code IDs; no free-form SQL or provider fields.
    selected = codes.split(",")
    try:
        return run_index_basket(
            codes=selected,
            start=start,
            end=end,
            window=window,
            quality_policy=quality_policy,
        )
    except AnalyticsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
