"""Bounded local Trend & Momentum evaluation for pinned price-index snapshots."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, localcontext
from pathlib import Path

from finintel.market_data.analytics import (
    AnalyticsError,
    Observation,
    _simple,
    normalized_level,
    relative_strength_ratio,
    rolling_rsi,
    shared_session_dates,
    validated_observations,
)
from finintel.market_data.analytics_store import _quality_status, _read_series
from finintel.models.contracts import (
    AnalysisEvidence,
    AnalysisRequest,
    AnalysisResult,
    ModelIdentity,
    ObservationRange,
    OutputValue,
    QualityEvidence,
    SourceEvidence,
    SourcePin,
)
from finintel.models.registry import (
    EmaParameters,
    MomentumParameters,
    RelativeStrengthParameters,
    RsiParameters,
    get_model_definition,
)
from finintel.storage.db import StorageError, _read_connection, database_path

Point = dict[str, OutputValue]
# Bound fixed-point expansion from corrupt local data before formatting, even if
# SQLite contains a short scientific-notation string for a huge exponent.
MAX_DECIMAL_CHARACTERS = 256
_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class _Input:
    pin: SourcePin
    rows: list[Observation]
    quality: QualityEvidence


def _fixed(value: Decimal | None) -> str | None:
    if value is None:
        return None
    representation = value.as_tuple()
    digits = len(representation.digits)
    places = representation.exponent
    length = max(1, digits + places) + (1 - places if places < 0 else 0)
    if length + representation.sign > MAX_DECIMAL_CHARACTERS:
        raise AnalyticsError("Fixed-point model result exceeds the numeric representation limit")
    return format(value, "f")


def _point(row: Observation) -> Point:
    return {"session_date": row.session_date.isoformat(), "close": _fixed(row.close)}


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
                "fast_ema": _fixed(fast),
                "slow_ema": _fixed(slow),
                "ema_difference": _fixed(difference),
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
                "comparison_close": _fixed(comparison.close) if comparison else None,
                "momentum": _fixed(_simple(comparison.close, row.close)) if comparison else None,
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
                "rsi": _fixed(rsi),
                "rsi_state": state,
                "lower_threshold": parameters.lower_threshold,
                "upper_threshold": parameters.upper_threshold,
                "warmup_complete": rsi is not None,
            }
        )
    return tuple(points)


def _relative_points(
    inputs: list[_Input], sessions: list[date], output_names: tuple[str, ...]
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
            point[f"{code}_normalized_100"] = _fixed(normalized_level(close, seed))
            point[f"{code}_relative_strength"] = _fixed(
                relative_strength_ratio(close, seed, benchmark_close, benchmark_seed)
            )
            point[f"{code}_return_difference"] = _fixed(_simple(seed, close) - benchmark_return)
        points.append(point)
    return tuple(points)


def _read_inputs(request: AnalysisRequest, db: Path | None, maximum: int) -> list[_Input]:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        raise AnalyticsError("No local stored observations are available")
    try:
        inputs = []
        for pin in request.sources:
            source = pin.identity
            identity, rows, provenance = _read_series(
                conn,
                kind=source.instrument_kind,
                code=source.code,
                provider=source.provider,
                start=request.window.start,
                end=request.window.end,
                max_observations=maximum,
            )
            if asdict(identity) != source.model_dump():
                raise AnalyticsError("Stored identity does not match the pinned price-index source")
            if provenance["snapshot_sha256"] != pin.snapshot_sha256:
                raise AnalyticsError("Local source snapshot changed; refresh analysis inputs")
            quality = _quality_status(conn, [source.code], precise_scan_status=True)
            if quality["quality_verified"]:
                evidence = QualityEvidence(status="verified")
            elif request.quality_policy == "require_complete":
                raise AnalyticsError("Current complete Data Quality scan required for each source")
            else:
                match quality["status"]:
                    case "not_run":
                        evidence = QualityEvidence(
                            status="not_run", warnings=("quality_scan_not_run",)
                        )
                    case "partial":
                        evidence = QualityEvidence(
                            status="partial", warnings=("quality_scan_partial",)
                        )
                    case "complete":
                        evidence = QualityEvidence(
                            status="blocked", warnings=("unresolved_quality_findings",)
                        )
                    case "stale":
                        evidence = QualityEvidence(status="stale", warnings=("quality_scan_stale",))
                    case _:
                        raise AnalyticsError("Unsupported Data Quality scan status")
            inputs.append(_Input(pin, validated_observations(rows), evidence))
        return inputs
    except sqlite3.Error as exc:
        raise StorageError("Local trend model query failed") from exc
    finally:
        conn.close()


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

    inputs = _read_inputs(request, db, model.observations.maximum)
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
        with localcontext(_DECIMAL_CONTEXT):
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

    evidence = AnalysisEvidence(
        model=ModelIdentity(
            model_id=model_id,
            model_version=model.model_version,
            implementation_revision=model.implementation_revision,
        ),
        calculation_version=model.calculation_version,
        sources=tuple(
            SourceEvidence(
                identity=source.pin.identity,
                snapshot_sha256=source.pin.snapshot_sha256,
                observed=ObservationRange(
                    start=source.rows[0].session_date,
                    end=source.rows[-1].session_date,
                    count=len(source.rows),
                ),
                aligned_count=len(sessions),
                excluded_count=len(source.rows) - len(sessions),
                quality=source.quality,
            )
            for source in inputs
        ),
        requested=request.window,
        observed_sessions=tuple(sessions),
        parameters=parameters.model_dump(),
        output_fields=model.output_fields,
        quality_policy=request.quality_policy,
        warnings=("quality_override",) if request.quality_policy == "allow_unverified" else (),
    )
    result = AnalysisResult.from_evaluation(
        evidence=evidence,
        points=points,
        evaluated_at_utc=evaluated_at_utc if evaluated_at_utc is not None else datetime.now(UTC),
    )
    if len(result.model_dump_json().encode("utf-8")) > model.execution.max_result_bytes:
        raise AnalyticsError("Trend model result exceeds the output size limit")
    definition.validate_result(request, result)
    return result
