"""Independent as-of price trend and realized-volatility classifications."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from itertools import pairwise
from pathlib import Path
from statistics import stdev

from finintel.market_data.analytics import (
    ANNUAL_SESSIONS,
    AnalyticsError,
    Observation,
    _simple,
    linear_empirical_quantile,
    trailing_sma,
)
from finintel.models.contracts import AnalysisRequest, AnalysisResult
from finintel.models.evaluation import (
    MODEL_DECIMAL_CONTEXT,
    Point,
    build_result,
    fixed_decimal,
    read_pinned_inputs,
)
from finintel.models.registry import REGIME_MODEL_ID, RegimeParameters, get_model_definition


def _trend_label(
    close_distance: Decimal | None, sma_distance: Decimal | None, band: Decimal, verified: bool
) -> tuple[str, str | None]:
    if close_distance is None or sma_distance is None:
        return "unknown", "trend_insufficient_history"
    if not verified:
        return "unknown", None
    if close_distance > band and sma_distance > band:
        return "uptrend", "trend_above_band"
    if close_distance < -band and sma_distance < -band:
        return "downtrend", "trend_below_band"
    return "sideways", "trend_mixed"


def _volatility_label(
    current: Decimal | None, lower: Decimal | None, upper: Decimal | None, verified: bool
) -> tuple[str, str | None]:
    if current is None or lower is None or upper is None:
        return "unknown", "vol_calibration_insufficient"
    if not verified:
        return "unknown", None
    if current < lower:
        return "low", "vol_below_p25"
    if current > upper:
        return "high", "vol_above_p75"
    return "normal", "vol_normal_range"


def _regime_sma(prices: list[Decimal], index: int, window: int) -> Decimal | None:
    if index + 1 < window:
        return None
    closing_level = prices[index]
    trailing_prices = prices[index + 1 - window : index + 1]
    # Preserve the exact level of a flat window. Summing repeated high-precision
    # levels at 28 digits can round differently for the short and long windows.
    if trailing_prices.count(closing_level) == window:
        return closing_level
    return trailing_sma(prices, index, window)


def _regime_points(
    rows: list[Observation], parameters: RegimeParameters, *, verified: bool
) -> tuple[Point, ...]:
    prices = [row.close for row in rows]
    returns = [_simple(left, right) for left, right in pairwise(prices)]
    band = Decimal(parameters.neutrality_band)
    annualizer = Decimal(ANNUAL_SESSIONS).sqrt()
    prior: deque[Decimal] = deque(maxlen=parameters.calibration_window)
    points = []

    for index, row in enumerate(rows):
        short = _regime_sma(prices, index, parameters.short_window)
        long = _regime_sma(prices, index, parameters.long_window)
        close_distance = _simple(long, row.close) if long is not None else None
        sma_distance = _simple(long, short) if long is not None and short is not None else None
        trend_state, trend_reason = _trend_label(close_distance, sma_distance, band, verified)

        start = index - parameters.volatility_window
        current = stdev(returns[start:index]) * annualizer if start >= 0 else None
        lower = upper = None
        if len(prior) == parameters.calibration_window:
            baseline = sorted(prior)
            lower = linear_empirical_quantile(baseline, Decimal("0.25"))
            upper = linear_empirical_quantile(baseline, Decimal("0.75"))
        volatility_state, volatility_reason = _volatility_label(current, lower, upper, verified)

        points.append(
            {
                "session_date": row.session_date.isoformat(),
                "close": fixed_decimal(row.close),
                "sma50": fixed_decimal(short),
                "sma200": fixed_decimal(long),
                "close_distance": fixed_decimal(close_distance),
                "sma_distance": fixed_decimal(sma_distance),
                "trend_state": trend_state,
                "realized_volatility": fixed_decimal(current),
                "volatility_p25": fixed_decimal(lower),
                "volatility_p75": fixed_decimal(upper),
                "volatility_calibration_count": len(prior),
                "volatility_state": volatility_state,
                "reason_codes": tuple(
                    code
                    for code in (
                        trend_reason,
                        volatility_reason,
                        None if verified else "quality_unverified",
                    )
                    if code is not None
                ),
            }
        )
        if current is not None:
            prior.append(current)

    return tuple(points)


def evaluate_regime_model(
    request: AnalysisRequest, *, db: Path | None = None, evaluated_at_utc: datetime | None = None
) -> AnalysisResult:
    """Evaluate historical market regimes through an internal read-only entry point."""
    request = AnalysisRequest.model_validate_json(request.model_dump_json())
    if request.model_id != REGIME_MODEL_ID:
        raise AnalyticsError("Unsupported Market Regime model")
    definition = get_model_definition(request)
    parameters = definition.validate_request(request)
    assert isinstance(parameters, RegimeParameters)
    inputs = read_pinned_inputs(
        request, db, definition.metadata.observations.maximum, family="Regime"
    )
    rows = inputs[0].rows
    if not rows:
        raise AnalyticsError("No local stored observations are available in the requested range")
    try:
        with localcontext(MODEL_DECIMAL_CONTEXT):
            points = _regime_points(
                rows, parameters, verified=request.quality_policy == "require_complete"
            )
    except DecimalException as exc:
        raise AnalyticsError("Regime model calculation is outside the numeric range") from exc
    return build_result(
        request,
        definition,
        parameters,
        inputs,
        [row.session_date for row in rows],
        points,
        evaluated_at_utc,
        family="Regime",
    )
