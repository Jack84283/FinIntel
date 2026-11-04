"""Deterministic, provider-specific local market analytics.

All calculations operate on actual dated observations, never generated sessions.
Decimal is used at the price/return boundary; statistical functions explicitly
convert to IEEE-754 binary64. Null means insufficient warm-up observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from statistics import mean, stdev

CALCULATION_VERSION = "index-analytics-v2"
MAX_OBSERVATIONS = 10_000
ALLOWED_WINDOWS = (5, 10, 14, 20, 21, 50, 63, 126, 200, 252)
ANNUAL_SESSIONS = 252


class AnalyticsError(ValueError):
    """Invalid series contract or incompatible analytical assumptions."""


@dataclass(frozen=True)
class SeriesIdentity:
    instrument_kind: str
    code: str
    provider: str
    adjustment: str
    return_semantics: str
    frequency: str
    units: str


@dataclass(frozen=True)
class Observation:
    session_date: date
    close: Decimal


def _number(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AnalyticsError("Invalid finite closing level") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AnalyticsError("Price and index levels must be finite and positive")
    return parsed


def validated_observations(rows: list[tuple[str, str]]) -> list[Observation]:
    if len(rows) > MAX_OBSERVATIONS:
        raise AnalyticsError("Requested series exceeds the 10,000-observation cap")
    result: list[Observation] = []
    for raw_date, raw_close in rows:
        try:
            day = date.fromisoformat(raw_date)
        except (TypeError, ValueError) as exc:
            raise AnalyticsError("Invalid market session date") from exc
        if day.isoformat() != raw_date or result and day <= result[-1].session_date:
            raise AnalyticsError("Market sessions must be strictly increasing and unique")
        result.append(Observation(day, _number(raw_close)))
    return result


def _decimal(value: Decimal | float) -> str:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnalyticsError("Non-finite calculation result")
        return format(value, ".15g")
    with localcontext() as ctx:
        ctx.prec = 28
        return format(+value, "f")


def _simple(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 28
        return right / left - Decimal(1)


def _log(left: Decimal, right: Decimal) -> float:
    return math.log(float(right / left))


def _sample_stats(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        raise AnalyticsError("At least two aligned returns are required")
    return mean(values), stdev(values)


def _period_returns(points: list[Observation], period: str) -> list[dict]:
    if period not in {"week", "month", "year"}:
        raise AnalyticsError("Unsupported return aggregation period")
    groups: list[tuple[tuple, Observation]] = []
    for item in points:
        d = item.session_date
        key = (
            (d.isocalendar().year, d.isocalendar().week)
            if period == "week"
            else (d.year, d.month)
            if period == "month"
            else (d.year,)
        )
        if groups and groups[-1][0] == key:
            groups[-1] = (key, item)
        else:
            groups.append((key, item))
    return [
        {
            "session_date": groups[index][1].session_date.isoformat(),
            "return": _decimal(_simple(groups[index - 1][1].close, groups[index][1].close)),
            "period": period,
        }
        for index in range(1, len(groups))
    ]


def _drawdown_recovery(observations: list[Observation]) -> tuple[list[dict], int | None, int | None]:
    """Peak/trough/recovery from observed sessions only, with censored open episodes.

    The first observed close at or above a prior peak completes recovery.
    Right-censored episodes do not fabricate a future recovery duration.
    """
    if len(observations) < 2:
        return [], None, None
    episodes: list[dict] = []
    peak_index = trough_index = 0
    longest_completed: int | None = None
    for index in range(1, len(observations)):
        level = observations[index].close
        peak = observations[peak_index].close
        if level >= peak:
            if trough_index > peak_index:
                duration = index - peak_index
                episodes.append(
                    {
                        "status": "recovered",
                        "peak_session_date": observations[peak_index].session_date.isoformat(),
                        "trough_session_date": observations[trough_index].session_date.isoformat(),
                        "recovery_session_date": observations[index].session_date.isoformat(),
                        "trough_drawdown": _decimal(
                            _simple(peak, observations[trough_index].close)
                        ),
                        "underwater_observed_sessions": duration - 1,
                        "peak_to_recovery_observed_sessions": duration,
                        "trough_to_recovery_observed_sessions": index - trough_index,
                    }
                )
                longest_completed = max(longest_completed or 0, duration)
            peak_index = trough_index = index
        elif trough_index == peak_index or level < observations[trough_index].close:
            trough_index = index
    ongoing: int | None = None
    if trough_index > peak_index:
        ongoing = len(observations) - 1 - peak_index
        episodes.append(
            {
                "status": "ongoing",
                "peak_session_date": observations[peak_index].session_date.isoformat(),
                "trough_session_date": observations[trough_index].session_date.isoformat(),
                "recovery_session_date": None,
                "trough_drawdown": _decimal(
                    _simple(observations[peak_index].close, observations[trough_index].close)
                ),
                "underwater_observed_sessions": ongoing,
                "peak_to_recovery_observed_sessions": None,
                "trough_to_recovery_observed_sessions": None,
            }
        )
    return episodes, longest_completed, ongoing


def compute_series(
    identity: SeriesIdentity,
    observations: list[Observation],
    *,
    window: int = 20,
    allow_volatility_returns: bool = False,
) -> dict:
    """Return absolute levels always; price-return metrics for eligible semantics.

    Simple daily: P_t / P_(t-1) - 1; log: ln(P_t / P_(t-1)).
    Rolling vol: sample stddev(simple daily returns, ddof=1) * sqrt(252).
    Moving average: mean of levels over trailing W actual observations.
    RSI: simple rolling W-session gains/(gains+losses), not Wilder smoothing.
    """
    if window not in ALLOWED_WINDOWS:
        raise AnalyticsError("Unsupported window; use a configured session count")
    if len(observations) > MAX_OBSERVATIONS:
        raise AnalyticsError("Too many observations")
    if identity.frequency not in {"1d", "daily"}:
        raise AnalyticsError("Only stored daily series are supported")
    allowed = identity.return_semantics in {
        "price_index",
        "raw_unadjusted_equity_price_usd_per_share",
    } or (identity.return_semantics == "volatility_index" and allow_volatility_returns)
    if identity.return_semantics not in {
        "price_index",
        "raw_unadjusted_equity_price_usd_per_share",
        "volatility_index",
    }:
        raise AnalyticsError("Unsupported source return semantics")
    dates = [item.session_date.isoformat() for item in observations]
    prices = [item.close for item in observations]
    returns = [_simple(prices[i - 1], prices[i]) for i in range(1, len(prices))]
    log_returns = [_log(prices[i - 1], prices[i]) for i in range(1, len(prices))]
    peak: Decimal | None = None
    worst = Decimal(0)
    longest_unrecovered = 0
    current_underwater = 0
    points = []
    for i, (day, level) in enumerate(zip(dates, prices, strict=True)):
        if peak is None or level >= peak:
            peak, current_underwater = level, 0
        else:
            current_underwater += 1
            longest_unrecovered = max(longest_unrecovered, current_underwater)
        drawdown = level / peak - 1
        worst = min(worst, drawdown)
        # On observation i, W returns require W+1 levels.
        roll = [float(v) for v in returns[i - window : i]] if i >= window else []
        ma = sum(prices[i - window + 1 : i + 1]) / window if i + 1 >= window else None
        momentum = _simple(prices[i - window], level) if i >= window else None
        rsi = None
        if i >= window:
            differences = [prices[j] - prices[j - 1] for j in range(i - window + 1, i + 1)]
            gains = sum((max(value, Decimal(0)) for value in differences), Decimal(0))
            losses = sum((-min(value, Decimal(0)) for value in differences), Decimal(0))
            rsi = Decimal(50) if gains + losses == 0 else Decimal(100) * gains / (gains + losses)
        points.append(
            {
                "session_date": day,
                "close": _decimal(level),
                "normalized_100": _decimal(level / prices[0] * 100) if prices else None,
                "simple_return": _decimal(returns[i - 1]) if allowed and i else None,
                "log_return": _decimal(log_returns[i - 1]) if allowed and i else None,
                "cumulative_return": _decimal(_simple(prices[0], level)) if allowed else None,
                "drawdown": _decimal(drawdown) if allowed else None,
                "rolling_volatility_annualized": (
                    _decimal(stdev(roll) * math.sqrt(ANNUAL_SESSIONS))
                    if allowed and len(roll) >= 2
                    else None
                ),
                "moving_average": _decimal(ma) if ma is not None else None,
                "momentum": _decimal(momentum) if allowed and momentum is not None else None,
                "rsi": _decimal(rsi) if allowed and rsi is not None else None,
            }
        )
    period = (
        {name: _period_returns(observations, name) for name in ("week", "month", "year")}
        if allowed
        else {"week": [], "month": [], "year": []}
    )
    episodes, longest_recovery, ongoing_duration = (
        _drawdown_recovery(observations) if allowed else ([], None, None)
    )
    return {
        "calculation_version": CALCULATION_VERSION,
        "identity": identity.__dict__,
        "settings": {
            "window_sessions": window,
            "annualization_sessions": ANNUAL_SESSIONS,
            "sample_stddev_ddof": 1,
            "missing_data_policy": "observed_sessions_only",
            "numeric_boundary": "Decimal prices/returns, float sample statistics",
            "volatility_returns_explicit": allow_volatility_returns,
        },
        "observations": len(points),
        "first_session": dates[0] if dates else None,
        "last_session": dates[-1] if dates else None,
        "metrics_enabled": allowed,
        "max_drawdown": _decimal(worst) if allowed and points else None,
        "longest_underwater_observed_sessions": longest_unrecovered if allowed and points else None,
        "longest_completed_recovery_observed_sessions": longest_recovery,
        "ongoing_drawdown_observed_sessions": ongoing_duration,
        "drawdown_episodes": episodes,
        "period_returns": period,
        "points": points,
    }


def compare_series(
    left: SeriesIdentity,
    left_rows: list[Observation],
    right: SeriesIdentity,
    right_rows: list[Observation],
    *,
    window: int = 20,
    missing_policy: str = "intersection",
) -> dict:
    """Pairwise returns use only consecutive *common* sessions, never forward fill."""
    if missing_policy not in {"intersection", "strict"}:
        raise AnalyticsError("Comparison policy must be intersection or strict")
    if window not in ALLOWED_WINDOWS:
        raise AnalyticsError("Unsupported window")
    kinds = {left.return_semantics, right.return_semantics}
    if "volatility_index" in kinds:
        raise AnalyticsError("VIX/volatility levels are not comparable equity price returns")
    if not kinds.issubset({"price_index", "raw_unadjusted_equity_price_usd_per_share"}):
        raise AnalyticsError("Incompatible adjustment or total-return semantics")
    if left.frequency not in {"1d", "daily"} or right.frequency not in {"1d", "daily"}:
        raise AnalyticsError("Comparison requires daily observations")
    if (
        left.instrument_kind == right.instrument_kind
        and left.code == right.code
        and left.provider == right.provider
    ):
        raise AnalyticsError("A series cannot be compared to itself")
    left_map = {point.session_date: point.close for point in left_rows}
    right_map = {point.session_date: point.close for point in right_rows}
    if missing_policy == "strict" and set(left_map) != set(right_map):
        raise AnalyticsError("Strict comparison requires identical observed session dates")
    common = sorted(left_map.keys() & right_map.keys())
    if len(common) < 2:
        raise AnalyticsError("At least two overlapping observations are required")
    lvals, rvals = [left_map[d] for d in common], [right_map[d] for d in common]
    left_r = [float(_simple(lvals[i - 1], lvals[i])) for i in range(1, len(common))]
    right_r = [float(_simple(rvals[i - 1], rvals[i])) for i in range(1, len(common))]
    points = []
    for i, day in enumerate(common):
        x = left_r[max(0, i - window) : i]
        y = right_r[max(0, i - window) : i]
        corr = beta = tracking = cov = None
        if i >= window and len(x) >= 2:
            mx, sx = _sample_stats(x)
            my, sy = _sample_stats(y)
            cov = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (len(x) - 1)
            corr = cov / (sx * sy) if sx and sy else None
            beta = cov / (sy * sy) if sy else None
            tracking = stdev([a - b for a, b in zip(x, y, strict=True)]) * math.sqrt(
                ANNUAL_SESSIONS
            )
        points.append(
            {
                "session_date": day.isoformat(),
                "left_normalized_100": _decimal(lvals[i] / lvals[0] * 100),
                "right_normalized_100": _decimal(rvals[i] / rvals[0] * 100),
                "left_return": _decimal(left_r[i - 1]) if i else None,
                "right_return": _decimal(right_r[i - 1]) if i else None,
                "relative_strength": _decimal((lvals[i] / lvals[0]) / (rvals[i] / rvals[0])),
                "rolling_correlation": _decimal(corr) if corr is not None else None,
                "rolling_covariance": _decimal(cov) if cov is not None else None,
                "rolling_beta": _decimal(beta) if beta is not None else None,
                "rolling_tracking_error_annualized": (
                    _decimal(tracking) if tracking is not None else None
                ),
            }
        )
    return {
        "calculation_version": CALCULATION_VERSION,
        "left": left.__dict__,
        "right": right.__dict__,
        "missing_data_policy": missing_policy,
        "overlap_sessions": len(common),
        "left_only_sessions": len(left_rows) - len(common),
        "right_only_sessions": len(right_rows) - len(common),
        "window_sessions": window,
        "annualization_sessions": ANNUAL_SESSIONS,
        "price_return_only": True,
        "dividends_excluded": "raw_unadjusted_equity_price_usd_per_share" in kinds,
        "notes": [
            "Returns use consecutive intersected observations; gaps are not filled.",
            "Price-return comparisons exclude cash dividends and may not reflect total return.",
        ],
        "points": points,
    }
