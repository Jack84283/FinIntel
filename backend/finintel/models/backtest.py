"""Phase 2C1: deterministic, offline *research* backtesting of pinned SMA signals.

No network, SQLite writes, orders, custom-code loading, or exchange fills.
FRED price-index levels are not directly tradable: next-observed-session
close is a hypothetical valuation/fill convention, NOT a demonstrated fill.
This module has no API endpoint; persisted jobs/UI are separate later phases.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finintel.models.engine import _sha256
from finintel.models.registry import (
    IMPLEMENTATION_REVISION,
    MAX_MODEL_OBSERVATIONS,
    MODEL_ID,
    MODEL_VERSION,
    ModelParameters,
)

BACKTEST_CONTRACT = "local-backtest-research-v1"
BACKTEST_REVISION = "sma-next-observed-close-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRICE = re.compile(r"^\d+(?:\.\d+)?$")
_SESSIONS_PER_YEAR = Decimal(252)
_BPS = Decimal(10000)


class BacktestContractError(ValueError):
    """Invalid data, unsupported semantics or unverifiable backtest inputs."""


class BacktestAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    initial_capital: Decimal = Field(default=Decimal("10000"), gt=0, le=Decimal("1e12"))
    position_fraction: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    commission_bps: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    risk_free_rate_annual: Decimal = Field(default=Decimal("0"), ge=Decimal("-0.99"), le=1)
    execution: Literal["next_observed_session_close"] = "next_observed_session_close"
    sizing: Literal["fixed_fraction_long_or_flat"] = "fixed_fraction_long_or_flat"
    benchmark: Literal["frictionless_same_index_buy_hold"] = "frictionless_same_index_buy_hold"
    rebalance: Literal["on_signal_change"] = "on_signal_change"
    annualization_sessions: Literal[252] = 252
    no_orders: Literal[True] = True

    @model_validator(mode="after")
    def finite_assumptions(self) -> BacktestAssumptions:
        for amount in (
            self.initial_capital,
            self.position_fraction,
            self.commission_bps,
            self.slippage_bps,
            self.risk_free_rate_annual,
        ):
            if not amount.is_finite():
                raise ValueError("Backtesting assumptions must be finite")
        return self


class BacktestRequest(BaseModel):
    """Bounded, versioned, source-pinned window; no arbitrary execution input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_id: Literal["builtin.sma_crossover"] = MODEL_ID
    model_version: Literal["1.0.0"] = MODEL_VERSION
    index_code: Literal["SP500", "DJIA", "NASDAQCOM", "NDX"]
    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_end: date
    test_start: date
    test_end: date
    source_semantics: Literal["fred_price_index_level"] = "fred_price_index_level"
    point_in_time_membership: Literal["not_used"] = "not_used"
    assumptions: BacktestAssumptions = Field(default_factory=BacktestAssumptions)

    @model_validator(mode="after")
    def separate_windows(self) -> BacktestRequest:
        if not self.training_end < self.test_start < self.test_end:
            raise ValueError("Training must end before at least two test sessions")
        return self


def _fail(reason: str) -> None:
    raise BacktestContractError(reason)


def _positive_decimal(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 80 or not _PRICE.fullmatch(value):
        _fail("Invalid price or moving-average decimal representation")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BacktestContractError("Invalid price decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        _fail("Price and moving averages must be positive finite numbers")
    return parsed


def _session(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("Invalid observed-session date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BacktestContractError("Invalid observed-session date") from exc
    if parsed.isoformat() != value:
        _fail("Noncanonical observed-session date")
    return parsed


def _fmt(value: Decimal) -> str:
    return format(+value, "f")


def _validated_points(research: dict, request: BacktestRequest) -> tuple[list[dict], list[date], list[Decimal]]:
    if not isinstance(research, dict) or research.get("execution_contract") != "model-research-evaluation-v1":
        _fail("Unsupported versioned model-result contract")
    if (
        research.get("status") != "completed"
        or research.get("read_only") is not True
        or research.get("no_network") is not True
        or research.get("no_synthetic_sessions") is not True
        or research.get("signal_after_close_only") is not True
        or research.get("backtest_validated") is not False
        or research.get("persistent_job") is not False
    ):
        _fail("Model result lacks the required local causal research contract")

    lineage = research.get("lineage")
    if not isinstance(lineage, dict):
        _fail("Missing model lineage")
    identity = lineage.get("source_identity")
    if not isinstance(identity, dict) or any(
        identity.get(key) != value
        for key, value in {
            "instrument_kind": "index",
            "code": request.index_code,
            "provider": "fred",
            "adjustment": "price_index",
            "return_semantics": "price_index",
            "frequency": "daily",
            "units": "index_points",
        }.items()
    ):
        _fail("Unsupported source identity or adjusted/return semantics")
    if (
        lineage.get("model_id") != request.model_id
        or lineage.get("model_version") != request.model_version
        or lineage.get("implementation_revision") != IMPLEMENTATION_REVISION
        or lineage.get("source_snapshot_sha256") != request.expected_snapshot_sha256
        or lineage.get("point_in_time_membership") != "not_used"
        or lineage.get("dividends_included") is not False
        or lineage.get("corporate_action_adjustment") != "none_index_price_level"
        or lineage.get("quality_verified") is not True
        or research.get("quality_warning") is not None
    ):
        _fail("Unverified quality, unsupported constituent semantics or changed model snapshot")

    if not isinstance(lineage.get("analysis_calculation_version"), str):
        _fail("Missing source calculation version")
    if not isinstance(lineage.get("requested_start"), str) or not isinstance(
        lineage.get("requested_end"), str
    ):
        _fail("Missing source date range")
    requested_start = _session(lineage["requested_start"])
    requested_end = _session(lineage["requested_end"])
    if requested_start > requested_end or request.test_end > requested_end:
        _fail("Test window extends beyond pinned source coverage")

    parameters = lineage.get("parameters")
    if not isinstance(parameters, dict):
        _fail("Missing SMA parameters")
    try:
        params = ModelParameters.model_validate(parameters)
    except ValueError as exc:
        raise BacktestContractError("Unsupported SMA parameter contract") from exc

    points = research.get("points")
    if (
        not isinstance(points, list)
        or not params.slow_window + 1 <= len(points) <= MAX_MODEL_OBSERVATIONS
        or type(research.get("observations")) is not int
        or research["observations"] != len(points)
    ):
        _fail("Unbounded or inconsistent observed model sessions")
    if (
        not isinstance(research.get("output_sha256"), str)
        or not _SHA256.fullmatch(research["output_sha256"])
        or research["output_sha256"] != _sha256(points)
        or not isinstance(research.get("reproducibility_sha256"), str)
        or not _SHA256.fullmatch(research["reproducibility_sha256"])
        or research["reproducibility_sha256"] != _sha256(lineage)
    ):
        _fail("Stored model data or lineage hash mismatch")

    dates: list[date] = []
    levels: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 28
        for index, point in enumerate(points):
            if not isinstance(point, dict) or set(point) != {
                "session_date", "close", "fast_sma", "slow_sma", "signal_after_close"
            }:
                _fail("Unsupported model-point schema")
            session = _session(point["session_date"])
            if (dates and session <= dates[-1]) or not requested_start <= session <= requested_end:
                _fail("Unsorted, duplicate, fabricated or out-of-range session")
            dates.append(session)
            levels.append(_positive_decimal(point["close"]))

            fast = (
                sum(levels[-params.fast_window :], Decimal(0)) / Decimal(params.fast_window)
                if len(levels) >= params.fast_window
                else None
            )
            slow = (
                sum(levels[-params.slow_window :], Decimal(0)) / Decimal(params.slow_window)
                if len(levels) >= params.slow_window
                else None
            )
            expected_signal = (
                "long" if fast > slow else "flat"
            ) if fast is not None and slow is not None else None
            if point["signal_after_close"] != expected_signal:
                _fail("Noncausal or inconsistent post-close SMA signal")
            for field, expected in (("fast_sma", fast), ("slow_sma", slow)):
                raw = point[field]
                if expected is None:
                    if raw is not None:
                        _fail("SMA appears before its observed warm-up window")
                elif _positive_decimal(raw) != expected:
                    _fail("SMA is not determined by observed prior/current closes")

    if (
        lineage.get("first_observed_session") != dates[0].isoformat()
        or lineage.get("last_observed_session") != dates[-1].isoformat()
    ):
        _fail("Mismatched first/last pinned observed sessions")
    return points, dates, levels


def run_sma_backtest(*, research: dict, request: BacktestRequest) -> dict:
    """Simulate *hypothetical* index exposure, with no same-close signal fills.

    Post-close signal at t can only be executed at the *next observed close*,
    and participates in price movement strictly AFTER that close. The index
    itself is not directly tradable; the benchmark ignores transaction costs.
    This pure function does not authorize live trading or production acceptance.
    """
    if not isinstance(request, BacktestRequest):
        _fail("Typed, validated backtest request required")
    points, dates, levels = _validated_points(research, request)
    if request.training_end not in dates:
        _fail("Training endpoint is not a pinned observed session")
    test_indices = [
        index for index, session in enumerate(dates)
        if request.test_start <= session <= request.test_end
    ]
    if (
        len(test_indices) < 2
        or dates[test_indices[0]] != request.test_start
        or dates[test_indices[-1]] != request.test_end
        or test_indices[0] < 1
        or dates[test_indices[0] - 1] != request.training_end
        or points[test_indices[0] - 1]["signal_after_close"] is None
    ):
        _fail("Test requires adjacent observed training warm-up and two test sessions")

    with localcontext() as ctx:
        ctx.prec = 28
        settings = request.assumptions
        equity = settings.initial_capital
        benchmark = settings.initial_capital
        exposure = Decimal(0)
        peak = equity
        worst_drawdown = Decimal(0)
        total_cost = Decimal(0)
        turnover = Decimal(0)
        rows: list[dict] = []
        transactions: list[dict] = []
        daily_returns: list[Decimal] = []
        for step, index in enumerate(test_indices):
            prior_equity = equity
            if step:
                observed_return = levels[index] / levels[test_indices[step - 1]] - Decimal(1)
                equity *= Decimal(1) + exposure * observed_return
                benchmark *= Decimal(1) + observed_return
            preceding = points[index - 1]
            target = (
                settings.position_fraction
                if preceding["signal_after_close"] == "long"
                else Decimal(0)
            )
            changed = abs(target - exposure)
            cost = equity * changed * (
                settings.commission_bps + settings.slippage_bps
            ) / _BPS
            if changed:
                transactions.append({
                    "signal_as_of_session": preceding["session_date"],
                    "hypothetical_fill_session": dates[index].isoformat(),
                    "exposure_before": _fmt(exposure),
                    "exposure_after": _fmt(target),
                    "turnover_fraction": _fmt(changed),
                    "cost": _fmt(cost),
                })
                turnover += changed
                total_cost += cost
                equity -= cost
                exposure = target
            if equity > peak:
                peak = equity
            drawdown = equity / peak - Decimal(1)
            worst_drawdown = min(worst_drawdown, drawdown)
            daily_returns.append(equity / prior_equity - Decimal(1))
            rows.append({
                "session_date": dates[index].isoformat(),
                "equity_after_cost": _fmt(equity),
                "frictionless_benchmark_equity": _fmt(benchmark),
                "exposure_after_close": _fmt(exposure),
                "transaction_cost": _fmt(cost),
                "drawdown": _fmt(drawdown),
                "executed_from_prior_signal": preceding["session_date"],
            })

        sessions = len(daily_returns)
        mean = sum(daily_returns, Decimal(0)) / Decimal(sessions)
        variance = sum(
            ((value - mean) ** 2 for value in daily_returns), Decimal(0)
        ) / Decimal(sessions - 1)
        volatility = variance.sqrt() * _SESSIONS_PER_YEAR.sqrt()
        risk_free_daily = (
            (Decimal(1) + settings.risk_free_rate_annual)
            ** (Decimal(1) / _SESSIONS_PER_YEAR) - Decimal(1)
        )
        sharpe = (
            (mean - risk_free_daily) / variance.sqrt() * _SESSIONS_PER_YEAR.sqrt()
            if variance > 0
            else None
        )
        exponent = _SESSIONS_PER_YEAR / Decimal(sessions)
        metrics = {
            "cumulative_return": _fmt(equity / settings.initial_capital - 1),
            "annualized_return": _fmt((equity / settings.initial_capital) ** exponent - 1),
            "max_drawdown": _fmt(worst_drawdown),
            "annualized_volatility": _fmt(volatility),
            "sharpe": _fmt(sharpe) if sharpe is not None else None,
            "risk_free_rate_annual": _fmt(settings.risk_free_rate_annual),
            "turnover_fraction": _fmt(turnover),
            "transaction_cost_total": _fmt(total_cost),
            "transactions": len(transactions),
            "frictionless_benchmark_cumulative_return": _fmt(
                benchmark / settings.initial_capital - 1
            ),
            "relative_to_frictionless_benchmark": _fmt(equity / benchmark - 1),
        }
        inputs = {
            "backtest_contract": BACKTEST_CONTRACT,
            "engine_revision": BACKTEST_REVISION,
            "request": request.model_dump(mode="json"),
            "model_result_sha256": research["output_sha256"],
            "model_lineage_sha256": research["reproducibility_sha256"],
        }
        input_sha256 = _sha256(inputs)
        report = {
            "contract": BACKTEST_CONTRACT,
            "engine_revision": BACKTEST_REVISION,
            "input_sha256": input_sha256,
            "source_snapshot_sha256": request.expected_snapshot_sha256,
            "model_id": request.model_id,
            "model_version": request.model_version,
            "source_semantics": request.source_semantics,
            "point_in_time_membership": "not_used",
            "signal_timing": "after_observed_close",
            "execution_assumption": settings.execution,
            "tradeable_instrument": False,
            "research_only": True,
            "no_network": True,
            "no_orders": True,
            "persisted": False,
            "test_sessions": sessions,
            "rows": rows,
            "transactions": transactions,
            "metrics": metrics,
            "caution": (
                "Hypothetical FRED index-price-level exposure, not a tradable fill or "
                "dividend-inclusive return; benchmark ignores all transaction costs."
            ),
        }
        report["output_sha256"] = _sha256(report)
        return report
