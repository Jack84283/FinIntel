"""Bounded local Trend & Momentum evaluation for pinned price-index snapshots."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path

from finintel.market_data.analytics import (
    AnalyticsError,
    Observation,
    _simple,
    normalized_level,
    relative_strength_ratio,
    rolling_rsi,
    shared_session_dates,
)
from finintel.models.contracts import AnalysisRequest, AnalysisResult
from finintel.models.evaluation import (
    MODEL_DECIMAL_CONTEXT,
    PinnedInput,
    Point,
    build_result,
    fixed_decimal,
    read_pinned_inputs,
)
from finintel.models.registry import (
    EmaParameters,
    MomentumParameters,
    RelativeStrengthParameters,
    RsiParameters,
    get_model_definition,
)


def _point(row: Observation) -> Point:
    return {"session_date": row.session_date.isoformat(), "close": fixed_decimal(row.close)}


def _ema(prices: list[Decimal], window: int) -> list[Decimal | None]:
    if window == 1:
        return [+level for level in prices]
    alpha = Decimal(2) / (window + 1)
    # Shift the arithmetic mean around its first close so constant inputs have
    # identical seeds even when their sum would require rounding.
    first = prices[0]
    previous = first + sum((level - first for level in prices[:window]), Decimal(0)) / window
    values: list[Decimal | None] = [None] * (window - 1) + [previous]
    for level in prices[window:]:
        # Algebraically equivalent to alpha*level + (1-alpha)*previous.
        # This form preserves a constant path at the selected precision.
        previous += alpha * (level - previous)
        values.append(previous)
    return values


def _ema_points(rows: list[Observation], parameters: EmaParameters) -> tuple[Point, ...]:
    prices = [row.close for row in rows]
    fast_values = _ema(prices, parameters.fast_window)
    slow_values = _ema(prices, parameters.slow_window)
    previous_difference: Decimal | None = None
    points = []
    for row, fast, slow in zip(rows, fast_values, slow_values, strict=True):
        difference = trend = event = None
        if fast is not None and slow is not None:
            difference = fast - slow
            trend = "above" if difference > 0 else "below" if difference < 0 else "flat"
            event = "none"
            if previous_difference is not None:
                if previous_difference <= 0 < difference:
                    event = "above"
                elif previous_difference >= 0 > difference:
                    event = "below"
            previous_difference = difference
        points.append(
            {
                **_point(row),
                "fast_ema": fixed_decimal(fast),
                "slow_ema": fixed_decimal(slow),
                "ema_difference": fixed_decimal(difference),
                "trend_after_close": trend,
                "crossover_after_close": event,
                "warmup_complete": difference is not None,
            }
        )
    return tuple(points)


def _momentum_points(rows: list[Observation], parameters: MomentumParameters) -> tuple[Point, ...]:
    lookback = parameters.lookback_sessions
    points = []
    for index, row in enumerate(rows):
        comparison = rows[index - lookback] if index >= lookback else None
        points.append(
            {
                **_point(row),
                "comparison_session_date": comparison.session_date.isoformat()
                if comparison
                else None,
                "comparison_close": fixed_decimal(comparison.close) if comparison else None,
                "momentum": fixed_decimal(_simple(comparison.close, row.close))
                if comparison
                else None,
                "lookback_sessions": lookback,
                "warmup_complete": comparison is not None,
            }
        )
    return tuple(points)


def _rsi_points(rows: list[Observation], parameters: RsiParameters) -> tuple[Point, ...]:
    prices = [row.close for row in rows]
    points = []
    for index, row in enumerate(rows):
        rsi = rolling_rsi(prices, index, parameters.window)
        state = None
        if rsi is not None:
            if rsi <= parameters.lower_threshold:
                state = "low"
            elif rsi >= parameters.upper_threshold:
                state = "high"
            else:
                state = "neutral"
        points.append(
            {
                **_point(row),
                "rsi": fixed_decimal(rsi),
                "rsi_state": state,
                "lower_threshold": parameters.lower_threshold,
                "upper_threshold": parameters.upper_threshold,
                "warmup_complete": rsi is not None,
            }
        )
    return tuple(points)


def _relative_points(
    inputs: list[PinnedInput], sessions: list[date], output_names: tuple[str, ...]
) -> tuple[Point, ...]:
    levels = {
        source.pin.identity.code.lower(): {row.session_date: row.close for row in source.rows}
        for source in inputs
    }
    benchmark = next(iter(levels))
    first = sessions[0]
    benchmark_levels = levels[benchmark]
    points = []
    for session in sessions:
        benchmark_close = benchmark_levels[session]
        benchmark_seed = benchmark_levels[first]
        benchmark_return = _simple(benchmark_seed, benchmark_close)
        point: Point = dict.fromkeys(output_names)
        point.update(session_date=session.isoformat(), benchmark=benchmark)
        for code, by_day in levels.items():
            close, seed = by_day[session], by_day[first]
            point[f"{code}_normalized_100"] = fixed_decimal(normalized_level(close, seed))
            point[f"{code}_relative_strength"] = fixed_decimal(
                relative_strength_ratio(close, seed, benchmark_close, benchmark_seed)
            )
            point[f"{code}_return_difference"] = fixed_decimal(
                _simple(seed, close) - benchmark_return
            )
        points.append(point)
    return tuple(points)


def evaluate_trend_model(
    request: AnalysisRequest, *, db: Path | None = None, evaluated_at_utc: datetime | None = None
) -> AnalysisResult:
    """Evaluate an AL-02 model only from a consistent, read-only local snapshot."""
    request = AnalysisRequest.model_validate_json(request.model_dump_json())
    definition = get_model_definition(request)
    parameters = definition.validate_request(request)
    model = definition.metadata
    model_id = model.model_id
    if model_id not in {
        "builtin.ema_trend",
        "builtin.momentum",
        "builtin.relative_strength",
        "builtin.rsi_state",
    }:
        raise AnalyticsError("Unsupported Trend & Momentum model")

    inputs = read_pinned_inputs(request, db, model.observations.maximum, family="Trend")
    minimum = (
        definition.parameter_minimum(parameters)
        if definition.parameter_minimum is not None
        else model.observations.minimum
    )
    if any(len(source.rows) < minimum for source in inputs):
        raise AnalyticsError(f"At least {minimum} observed sessions per source are required")
    sessions = shared_session_dates([source.rows for source in inputs])
    if len(sessions) < minimum:
        raise AnalyticsError(f"At least {minimum} shared observed sessions are required")

    rows = inputs[0].rows
    try:
        with localcontext(MODEL_DECIMAL_CONTEXT):
            match model_id, parameters:
                case "builtin.ema_trend", EmaParameters():
                    points = _ema_points(rows, parameters)
                case "builtin.momentum", MomentumParameters():
                    points = _momentum_points(rows, parameters)
                case "builtin.relative_strength", RelativeStrengthParameters():
                    points = _relative_points(
                        inputs, sessions, tuple(field.name for field in model.output_fields)
                    )
                case "builtin.rsi_state", RsiParameters():
                    points = _rsi_points(rows, parameters)
                case _:
                    raise AnalyticsError("Invalid Trend & Momentum parameter contract")
    except DecimalException as exc:
        raise AnalyticsError("Trend model calculation is outside the numeric range") from exc

    return build_result(
        request, definition, parameters, inputs, sessions, points, evaluated_at_utc, family="Trend"
    )
