"""Bounded historical risk evaluation for ordered, pinned price-index sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from itertools import pairwise
from pathlib import Path
from statistics import mean, stdev

from finintel.market_data.analytics import (
    ANNUAL_SESSIONS,
    AnalyticsError,
    DrawdownTracker,
    Observation,
    _simple,
    sample_covariance,
    shared_session_dates,
)
from finintel.models.contracts import AnalysisRequest, AnalysisResult
from finintel.models.evaluation import (
    MODEL_DECIMAL_CONTEXT,
    Point,
    build_result,
    fixed_decimal,
    read_pinned_inputs,
)
from finintel.models.registry import (
    RISK_MODEL_ID,
    RiskParameters,
    get_model_definition,
)

_ZERO = Decimal(0)
_CONFIDENCE = Decimal("0.95")


def _historical_tail(returns: list[Decimal], window: int) -> Point:
    if len(returns) < window:
        return {
            "historical_sample_count": len(returns),
            "historical_tail_count": None,
            "historical_tail_status": "insufficient_history",
            "var_95": None,
            "expected_shortfall_95": None,
        }
    losses = sorted(-value for value in returns)
    rank = _CONFIDENCE * (len(losses) - 1)
    lower = int(rank)
    quantile = losses[lower] + (losses[lower + 1] - losses[lower]) * (rank - lower)
    threshold = max(_ZERO, quantile)
    tail = [loss for loss in losses if loss >= threshold]
    return {
        "historical_sample_count": len(losses),
        "historical_tail_count": len(tail),
        "historical_tail_status": "available" if tail else "empty_tail",
        "var_95": fixed_decimal(threshold),
        "expected_shortfall_95": fixed_decimal(mean(tail)) if tail else None,
    }


@dataclass
class _DrawdownHistory:
    tracker: DrawdownTracker = field(default_factory=DrawdownTracker)
    worst: Decimal = _ZERO
    worst_pair: tuple[int, int] | None = None
    latest_recovery: tuple[int, int, int] | None = None

    def point(self, rows: list[Observation], index: int) -> Point:
        recovered = self.tracker.advance(rows, index)
        if recovered is not None:
            self.latest_recovery = (*recovered, index)
        peak, trough = self.tracker.peak, self.tracker.trough
        drawdown = _simple(rows[peak].close, rows[index].close)
        if drawdown < self.worst:
            self.worst = drawdown
            self.worst_pair = (peak, index)
        ongoing = trough > peak
        worst_peak, worst_trough = self.worst_pair or (None, None)
        recovered_peak, recovered_trough, recovery = self.latest_recovery or (None, None, None)

        def session(position: int | None) -> str | None:
            return rows[position].session_date.isoformat() if position is not None else None

        return {
            "current_drawdown": fixed_decimal(drawdown),
            "maximum_drawdown": fixed_decimal(self.worst),
            "maximum_peak_date": session(worst_peak),
            "maximum_trough_date": session(worst_trough),
            "open_peak_date": session(peak) if ongoing else None,
            "open_trough_date": session(trough) if ongoing else None,
            "open_elapsed_intervals": index - peak if ongoing else None,
            "last_recovered_peak_date": session(recovered_peak),
            "last_recovered_trough_date": session(recovered_trough),
            "last_recovery_date": session(recovery),
            "last_peak_to_recovery_intervals": (
                recovery - recovered_peak
                if recovery is not None and recovered_peak is not None
                else None
            ),
            "last_trough_to_recovery_intervals": (
                recovery - recovered_trough
                if recovery is not None and recovered_trough is not None
                else None
            ),
        }


def _rolling_risk(primary: list[Decimal], comparison: list[Decimal], annualizer: Decimal) -> Point:
    sigma = stdev(primary)
    comparison_sigma = stdev(comparison) if comparison else None
    correlation = None
    if sigma and comparison_sigma:
        covariance = sample_covariance(primary, comparison, mean(primary), mean(comparison))
        correlation = max(Decimal(-1), min(Decimal(1), covariance / (sigma * comparison_sigma)))
    downside = sum(min(value, _ZERO) ** 2 for value in primary) / len(primary)
    return {
        "annualized_volatility": fixed_decimal(sigma * annualizer),
        "downside_deviation": fixed_decimal(downside.sqrt()),
        "correlation": fixed_decimal(correlation),
    }


def _returns(rows: list[Observation]) -> list[Decimal]:
    return [_simple(left.close, right.close) for left, right in pairwise(rows)]


def _risk_points(
    rows: list[Observation], comparison: list[Observation], parameters: RiskParameters
) -> tuple[Point, ...]:
    returns = _returns(rows)
    paired_returns = _returns(comparison)
    annualizer = Decimal(ANNUAL_SESSIONS).sqrt()
    drawdown = _DrawdownHistory()
    points = []
    for index, row in enumerate(rows):
        risk_start = index - parameters.risk_window
        rolling = (
            _rolling_risk(returns[risk_start:index], paired_returns[risk_start:index], annualizer)
            if risk_start >= 0
            else {"annualized_volatility": None, "downside_deviation": None, "correlation": None}
        )
        points.append(
            {
                "session_date": row.session_date.isoformat(),
                "close": fixed_decimal(row.close),
                "simple_return": fixed_decimal(returns[index - 1]) if index else None,
                "comparison_return": fixed_decimal(paired_returns[index - 1])
                if index and paired_returns
                else None,
                **rolling,
                **drawdown.point(rows, index),
                **_historical_tail(
                    returns[max(0, index - parameters.tail_window) : index], parameters.tail_window
                ),
            }
        )
    return tuple(points)


def evaluate_risk_model(
    request: AnalysisRequest, *, db: Path | None = None, evaluated_at_utc: datetime | None = None
) -> AnalysisResult:
    """Evaluate historical risk without routing through managed model execution."""
    request = AnalysisRequest.model_validate_json(request.model_dump_json())
    if request.model_id != RISK_MODEL_ID:
        raise AnalyticsError("Unsupported Risk Analysis model")
    definition = get_model_definition(request)
    parameters = definition.validate_request(request)
    assert isinstance(parameters, RiskParameters)
    inputs = read_pinned_inputs(
        request, db, definition.metadata.observations.maximum, family="Risk"
    )
    sessions = shared_session_dates([source.rows for source in inputs])
    if not sessions:
        raise AnalyticsError("At least one shared observed session is required")
    common = set(sessions)
    rows = [row for row in inputs[0].rows if row.session_date in common]
    comparison = (
        [row for row in inputs[1].rows if row.session_date in common] if len(inputs) == 2 else []
    )
    try:
        with localcontext(MODEL_DECIMAL_CONTEXT):
            points = _risk_points(rows, comparison, parameters)
    except DecimalException as exc:
        raise AnalyticsError("Risk model calculation is outside the numeric range") from exc
    return build_result(
        request, definition, parameters, inputs, sessions, points, evaluated_at_utc, family="Risk"
    )
