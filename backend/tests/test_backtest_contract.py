"""Pure synthetic Phase 2C1 engine regressions; no user DB, network or execution."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finintel.models.backtest import (
    BACKTEST_CONTRACT,
    BacktestAssumptions,
    BacktestContractError,
    BacktestRequest,
    run_sma_backtest,
)
from finintel.models.engine import _sha256
from finintel.models.registry import IMPLEMENTATION_REVISION

START = date(2026, 8, 1)
SOURCE_HASH = "a" * 64


def _research(*, closes: list[int] | None = None, skipped: set[int] | None = None) -> dict:
    prices = closes if closes is not None else [100 + i for i in range(45)]
    omitted = skipped or set()
    points: list[dict] = []
    levels: list[Decimal] = []
    for idx, close in enumerate(prices):
        if idx in omitted:
            continue
        levels.append(Decimal(close))
        fast = sum(levels[-5:], Decimal(0)) / Decimal(5) if len(levels) >= 5 else None
        slow = sum(levels[-20:], Decimal(0)) / Decimal(20) if len(levels) >= 20 else None
        signal = ("long" if fast > slow else "flat") if slow is not None else None
        points.append(
            {
                "session_date": (START + timedelta(days=idx)).isoformat(),
                "close": str(close),
                "fast_sma": format(fast, "f") if fast is not None else None,
                "slow_sma": format(slow, "f") if slow is not None else None,
                "signal_after_close": signal,
            }
        )
    lineage = {
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "parameters": {"fast_window": 5, "slow_window": 20},
        "source_identity": {
            "instrument_kind": "index",
            "code": "SP500",
            "provider": "fred",
            "adjustment": "price_index",
            "return_semantics": "price_index",
            "frequency": "daily",
            "units": "index_points",
        },
        "source_snapshot_sha256": SOURCE_HASH,
        "analysis_calculation_version": "index-analytics-v2",
        "requested_start": points[0]["session_date"],
        "requested_end": points[-1]["session_date"],
        "first_observed_session": points[0]["session_date"],
        "last_observed_session": points[-1]["session_date"],
        "quality_policy": "require_complete",
        "quality_status": "verified",
        "quality_verified": True,
        "point_in_time_membership": "not_used",
        "dividends_included": False,
        "corporate_action_adjustment": "none_index_price_level",
    }
    return {
        "execution_contract": "model-research-evaluation-v1",
        "lineage": lineage,
        "reproducibility_sha256": _sha256(lineage),
        "output_sha256": _sha256(points),
        "status": "completed",
        "observations": len(points),
        "points": points,
        "quality_warning": None,
        "read_only": True,
        "no_network": True,
        "no_synthetic_sessions": True,
        "signal_after_close_only": True,
        "backtest_validated": False,
        "persistent_job": False,
    }


def _request(*, last: int = 40, assumptions: BacktestAssumptions | None = None) -> BacktestRequest:
    return BacktestRequest(
        index_code="SP500",
        expected_snapshot_sha256=SOURCE_HASH,
        training_end=START + timedelta(days=29),
        test_start=START + timedelta(days=30),
        test_end=START + timedelta(days=last),
        assumptions=assumptions or BacktestAssumptions(),
    )


def _repin(result: dict) -> dict:
    result["reproducibility_sha256"] = _sha256(result["lineage"])
    result["output_sha256"] = _sha256(result["points"])
    return result


def test_next_observed_session_close_is_hypothetical_and_never_same_close():
    settings = BacktestAssumptions(
        initial_capital=Decimal(10000),
        commission_bps=Decimal(10),
        slippage_bps=Decimal(15),
    )
    result = run_sma_backtest(research=_research(), request=_request(assumptions=settings))
    assert result["contract"] == BACKTEST_CONTRACT
    assert result["tradeable_instrument"] is False
    assert result["no_orders"] is True
    assert result["no_network"] is True
    assert result["persisted"] is False
    assert result["research_only"] is True
    assert result["point_in_time_membership"] == "not_used"
    assert result["execution_assumption"] == "next_observed_session_close"
    assert result["test_sessions"] == 11
    first = result["rows"][0]
    assert first["session_date"] == (START + timedelta(days=30)).isoformat()
    assert first["executed_from_prior_signal"] == (START + timedelta(days=29)).isoformat()
    assert Decimal(first["equity_after_cost"]) == Decimal(9975)
    assert Decimal(first["frictionless_benchmark_equity"]) == Decimal(10000)
    assert Decimal(result["rows"][1]["equity_after_cost"]) > Decimal(9975)
    assert result["transactions"][0]["signal_as_of_session"] == first["executed_from_prior_signal"]
    assert result["transactions"][0]["hypothetical_fill_session"] == first["session_date"]
    assert result["metrics"]["transactions"] == 1
    assert Decimal(result["metrics"]["transaction_cost_total"]) == Decimal(25)


def test_reproducible_outputs_cost_monotonicity_and_benchmark_semantics():
    baseline = run_sma_backtest(research=_research(), request=_request())
    again = run_sma_backtest(research=_research(), request=_request())
    assert baseline == again
    assert baseline["output_sha256"] == again["output_sha256"]
    costs = BacktestAssumptions(commission_bps=Decimal(100))
    costly = run_sma_backtest(research=_research(), request=_request(assumptions=costs))
    assert Decimal(costly["metrics"]["cumulative_return"]) < Decimal(
        baseline["metrics"]["cumulative_return"]
    )
    assert (
        costly["metrics"]["frictionless_benchmark_cumulative_return"]
        == (baseline["metrics"]["frictionless_benchmark_cumulative_return"])
    )
    assert Decimal(costly["metrics"]["turnover_fraction"]) == Decimal(1)
    assert baseline["metrics"]["max_drawdown"] is not None
    assert baseline["metrics"]["annualized_volatility"] is not None
    assert baseline["metrics"]["risk_free_rate_annual"] == "0"


def test_no_future_leakage_and_no_generated_calendar_sessions():
    earlier = run_sma_backtest(research=_research(), request=_request(last=35))
    longer = run_sma_backtest(research=_research(), request=_request(last=40))
    assert earlier["rows"] == longer["rows"][: len(earlier["rows"])]

    # Revision strictly after the earlier test end must not alter earlier metrics
    # or path, even though the *full* source SHA would differ in a real ledger.
    altered = [100 + i for i in range(45)]
    altered[39] += 250
    modified = run_sma_backtest(research=_research(closes=altered), request=_request(last=35))
    assert earlier["rows"] == modified["rows"]
    assert earlier["metrics"] == modified["metrics"]

    missing = _research(skipped={34, 36})
    with pytest.raises(BacktestContractError):
        run_sma_backtest(research=missing, request=_request(last=36))
    observed_only = run_sma_backtest(research=missing, request=_request(last=35))
    assert observed_only["test_sessions"] == 5
    assert (START + timedelta(days=34)).isoformat() not in {
        row["session_date"] for row in observed_only["rows"]
    }


def test_refuses_bad_hashes_tampering_future_signals_and_wrong_semantics():
    original = _research()
    with pytest.raises(BacktestContractError, match="hash mismatch"):
        run_sma_backtest(research={**original, "output_sha256": "b" * 64}, request=_request())
    forged = _research()
    forged["points"][30]["signal_after_close"] = "flat"
    with pytest.raises(BacktestContractError, match="hash mismatch"):
        run_sma_backtest(research=forged, request=_request())
    with pytest.raises(BacktestContractError, match="Noncausal"):
        run_sma_backtest(research=_repin(forged), request=_request())

    for field, wrong in (
        ("return_semantics", "total_return"),
        ("adjustment", "split_adjusted"),
        ("provider", "unknown"),
    ):
        changed = _research()
        changed["lineage"]["source_identity"][field] = wrong
        with pytest.raises(BacktestContractError, match="Unsupported source"):
            run_sma_backtest(research=_repin(changed), request=_request())

    changed = _research()
    changed["lineage"]["quality_verified"] = False
    changed["quality_warning"] = "Unverified"
    with pytest.raises(BacktestContractError, match="Unverified"):
        run_sma_backtest(research=_repin(changed), request=_request())

    changed = _research()
    changed["lineage"]["point_in_time_membership"] = "inferred"
    with pytest.raises(BacktestContractError, match="constituent semantics"):
        run_sma_backtest(research=_repin(changed), request=_request())


def test_refuses_unobserved_boundary_unsorted_rows_and_inadequate_warmup():
    # Construct a freshly validated request with an invalid split.
    with pytest.raises(ValidationError):
        BacktestRequest(
            index_code="SP500",
            expected_snapshot_sha256=SOURCE_HASH,
            training_end=START + timedelta(days=30),
            test_start=START + timedelta(days=30),
            test_end=START + timedelta(days=31),
        )
    with pytest.raises(BacktestContractError, match="adjacent observed"):
        run_sma_backtest(
            research=_research(),
            request=BacktestRequest(
                index_code="SP500",
                expected_snapshot_sha256=SOURCE_HASH,
                training_end=START + timedelta(days=28),
                test_start=START + timedelta(days=30),
                test_end=START + timedelta(days=35),
            ),
        )

    unsorted = _research()
    unsorted["points"][30]["session_date"] = unsorted["points"][29]["session_date"]
    with pytest.raises(BacktestContractError, match="Unsorted"):
        run_sma_backtest(research=_repin(unsorted), request=_request())

    short = _research()
    with pytest.raises(BacktestContractError):
        run_sma_backtest(
            research=short,
            request=BacktestRequest(
                index_code="SP500",
                expected_snapshot_sha256=SOURCE_HASH,
                training_end=START + timedelta(days=18),
                test_start=START + timedelta(days=19),
                test_end=START + timedelta(days=21),
            ),
        )


def test_contract_forbids_leverage_unsupported_symbols_and_custom_execution():
    with pytest.raises(ValidationError):
        BacktestAssumptions(position_fraction=Decimal("1.2"))
    with pytest.raises(ValidationError):
        BacktestAssumptions(execution="same_close")
    with pytest.raises(ValidationError):
        BacktestAssumptions(commission_bps=Decimal(-1))
    with pytest.raises(ValidationError):
        BacktestRequest(
            index_code="VIX",
            expected_snapshot_sha256=SOURCE_HASH,
            training_end=START + timedelta(days=29),
            test_start=START + timedelta(days=30),
            test_end=START + timedelta(days=35),
        )
    with pytest.raises(ValidationError):
        BacktestRequest(
            index_code="SP500",
            expected_snapshot_sha256="x" * 64,
            training_end=START + timedelta(days=29),
            test_start=START + timedelta(days=30),
            test_end=START + timedelta(days=35),
        )
