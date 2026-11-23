"""Historical regime fixtures using disposable synthetic price snapshots."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_trend_models import TIMESTAMP, _day, _record_quality_scan, _request, _seed

from finintel.market_data.analytics import AnalyticsError, linear_empirical_quantile
from finintel.models import regime
from finintel.models.contracts import AnalysisResult
from finintel.models.registry import REGIME_DEFINITION, RegimeParameters

_SHORT = {"short_window": 1, "long_window": 2, "volatility_window": 2, "calibration_window": 2}


def _regime_request(db, *, policy="require_complete", **kwargs):
    return _request(db, "market_regime", quality_policy=policy, **kwargs)


def _evaluate(db, **kwargs):
    return regime.evaluate_regime_model(
        _regime_request(db, **kwargs), db=db, evaluated_at_utc=TIMESTAMP
    )


@pytest.mark.parametrize(
    ("prices", "band", "state", "reason", "distance"),
    [
        ([90, 110], "0.02", "uptrend", "trend_above_band", "0.1"),
        ([110, 90], "0.02", "downtrend", "trend_below_band", "-0.1"),
        ([96, 104], "0.04", "sideways", "trend_mixed", "0.04"),
        ([104, 96], "0.04", "sideways", "trend_mixed", "-0.04"),
        ([100, 100], "0", "sideways", "trend_mixed", "0"),
        ([96, 104], "0", "uptrend", "trend_above_band", "0.04"),
    ],
)
def test_trend_strict_boundaries_and_independent_warmup(
    tmp_path, prices, band, state, reason, distance
):
    db = tmp_path / "trend.sqlite"
    _seed(db, prices)
    _record_quality_scan(db, status="completed")
    initial, current = _evaluate(db, parameters={**_SHORT, "neutrality_band": band}).points
    assert initial["trend_state"] == "unknown"
    assert initial["sma50"] == str(prices[0])
    assert initial["sma200"] is initial["close_distance"] is initial["sma_distance"] is None
    assert initial["reason_codes"] == ("trend_insufficient_history", "vol_calibration_insufficient")
    assert current["trend_state"] == state
    assert current["reason_codes"] == (reason, "vol_calibration_insufficient")
    assert Decimal(current["sma200"]) == 100
    assert (
        Decimal(current["close_distance"]) == Decimal(current["sma_distance"]) == Decimal(distance)
    )
    assert current["volatility_state"] == "unknown"


def test_high_precision_flat_levels_remain_sideways_at_zero_band(tmp_path):
    db = tmp_path / "flat.sqlite"
    level = "9000.123456789012345678901234"
    _seed(db, [level] * 200)
    _record_quality_scan(db, status="completed")

    result = _evaluate(db, parameters={"neutrality_band": "0"})
    assert result.evidence.sources[0].quality.status == "verified"
    point = result.points[-1]
    assert point["close"] == point["sma50"] == point["sma200"] == level
    assert Decimal(point["close_distance"]) == Decimal(point["sma_distance"]) == 0
    assert point["trend_state"] == "sideways"
    assert point["reason_codes"] == ("trend_mixed", "vol_calibration_insufficient")


def test_mixed_trend_and_volatility_independent_of_long_sma(tmp_path):
    db = tmp_path / "mixed.sqlite"
    _seed(db, [100, 100, 130, 102, 112])
    _record_quality_scan(db, status="completed")
    initial, warmed = _evaluate(
        db, parameters={**_SHORT, "short_window": 2, "long_window": 5, "calibration_window": 1}
    ).points[3:]
    assert initial["trend_state"] == "unknown"
    assert initial["volatility_state"] in {"low", "normal", "high"}
    assert warmed["trend_state"] == "sideways"
    assert Decimal(warmed["close_distance"]) > Decimal("0.02")
    assert Decimal(warmed["sma_distance"]) < Decimal("0.02")


def test_prior_only_percentiles_and_sample_stddev_on_irregular_sessions(tmp_path):
    db = tmp_path / "volatility.sqlite"
    offsets = [0, 1, 3, 5, 6, 7, 10, 12]
    _seed(db, [100, 100, 100, 200, 400, 400, 400, 3200], offsets=offsets)
    _record_quality_scan(db, status="completed")
    full = _evaluate(db, parameters=_SHORT)
    assert tuple(p["session_date"] for p in full.points) == tuple(
        _day(offset).isoformat() for offset in offsets
    )
    assert full.points[1]["realized_volatility"] is None
    assert Decimal(full.points[2]["realized_volatility"]) == 0
    assert full.points[3]["volatility_calibration_count"] == 1
    assert full.points[3]["volatility_p25"] is full.points[3]["volatility_p75"] is None
    # Returns 0 and 1 at row 3 give sample variance 1/2, annualized by 252.
    shock = Decimal(126).sqrt()
    assert Decimal(full.points[3]["realized_volatility"]) == pytest.approx(shock)
    low = full.points[4]
    assert low["volatility_state"] == "low"
    assert low["reason_codes"] == ("trend_above_band", "vol_below_p25")
    assert Decimal(low["volatility_p25"]) == pytest.approx(shock / 4)
    assert Decimal(low["volatility_p75"]) == pytest.approx(shock * 3 / 4)
    assert full.points[-1]["volatility_state"] == "high"
    assert full.points[-1]["reason_codes"][-1] == "vol_above_p75"
    for length in range(1, len(offsets)):
        prefix = _evaluate(db, end=_day(offsets[length - 1]), parameters=_SHORT)
        assert prefix.points == full.points[:length]


def test_default_warmup_with_zero_variance_and_equal_thresholds(tmp_path):
    db = tmp_path / "warmup.sqlite"
    _seed(db, [100] * 821)
    _record_quality_scan(db, status="completed")
    result = _evaluate(db, end=_day(820))
    assert result.points[62]["realized_volatility"] is None
    assert Decimal(result.points[63]["realized_volatility"]) == 0
    assert result.points[198]["trend_state"] == "unknown"
    assert result.points[199]["trend_state"] == "sideways"
    assert result.points[818]["volatility_calibration_count"] == 755
    assert result.points[818]["volatility_state"] == "unknown"
    for point in result.points[819:]:
        assert point["volatility_calibration_count"] == 756
        assert Decimal(point["volatility_p25"]) == Decimal(point["volatility_p75"]) == 0
        assert point["volatility_state"] == "normal"
        assert point["reason_codes"] == ("trend_mixed", "vol_normal_range")


@pytest.mark.parametrize("boundary", [Decimal("0.1"), Decimal("0.2")])
def test_nonzero_threshold_equality_is_normal(boundary):
    assert regime._volatility_label(boundary, Decimal("0.1"), Decimal("0.2"), True) == (
        "normal",
        "vol_normal_range",
    )


def test_empirical_percentile_estimator():
    baseline = list(map(Decimal, [4, 8, 12, 44]))
    assert linear_empirical_quantile(baseline, Decimal("0.25")) == Decimal(7)
    assert linear_empirical_quantile(baseline, Decimal("0.75")) == Decimal(20)
    assert linear_empirical_quantile([Decimal(4)], Decimal("0.75")) == Decimal(4)


@pytest.mark.parametrize(
    "parameters",
    [
        {"short_window": 200},
        {"long_window": 2001},
        {"neutrality_band": 2},
        {"neutrality_band": "-0.01"},
        {"neutrality_band": "1.01"},
        {"volatility_window": 1},
        {"volatility_window": True},
        {"volatility_window": 1998, "calibration_window": 2},
        {"calibration_window": 1998},
        {"calibration_window": 0},
        {"extra": 0},
    ],
)
def test_parameters_reject_invalid_or_oversized(parameters):
    with pytest.raises(ValidationError):
        RegimeParameters.model_validate(parameters)


@pytest.mark.parametrize(("status", "expected"), [("partial", "partial"), ("stale", "stale")])
def test_incomplete_quality_has_unknown_labels(tmp_path, status, expected):
    db = tmp_path / "quality.sqlite"
    _seed(db, [100, 110, 120, 140, 160])
    _record_quality_scan(db, status=status)
    with pytest.raises(AnalyticsError, match="complete Data Quality scan"):
        _evaluate(db, parameters=_SHORT)
    unverified = _evaluate(db, policy="allow_unverified", parameters=_SHORT)
    assert unverified.evidence.sources[0].quality.status == expected
    assert unverified.evidence.warnings == ("quality_override",)
    assert unverified.points[-1]["reason_codes"] == ("quality_unverified",)
    assert (
        unverified.points[-1]["trend_state"]
        == unverified.points[-1]["volatility_state"]
        == "unknown"
    )
    assert unverified.points[-1]["volatility_p25"] is not None


def test_verified_scan_does_not_remove_explicit_quality_override(tmp_path):
    db = tmp_path / "override.sqlite"
    _seed(db, [100, 110])
    _record_quality_scan(db, status="completed")
    override = _evaluate(db, policy="allow_unverified", parameters=_SHORT)
    assert override.evidence.sources[0].quality.status == "verified"
    assert override.evidence.warnings == ("quality_override",)
    assert override.points[-1]["trend_state"] == "unknown"
    assert override.points[-1]["reason_codes"] == (
        "vol_calibration_insufficient",
        "quality_unverified",
    )


def test_source_pin_validation_deterministic_hashes_and_no_writes(tmp_path):
    db = tmp_path / "evidence.sqlite"
    _seed(db, [100, 101, 102, 103, 125])
    _record_quality_scan(db, status="completed")
    request = _regime_request(db, parameters=_SHORT)
    before = db.read_bytes()
    first = regime.evaluate_regime_model(request, db=db, evaluated_at_utc=TIMESTAMP)
    later = regime.evaluate_regime_model(
        request, db=db, evaluated_at_utc=TIMESTAMP + timedelta(days=1)
    )
    assert db.read_bytes() == before
    assert first.output_sha256 == later.output_sha256
    assert first.reproducibility_sha256 == later.reproducibility_sha256
    assert first.evidence.sources[0].snapshot_sha256 == request.sources[0].snapshot_sha256
    assert first.evidence.calculation_version == "market-regime-v1"
    assert AnalysisResult.model_validate_json(first.model_dump_json()) == first
    points = ({**first.points[0], "reason_codes": ("unexpected_reason",)}, *first.points[1:])
    with pytest.raises(ValidationError, match="declared output schema"):
        AnalysisResult.from_evaluation(
            evidence=first.evidence, points=points, evaluated_at_utc=TIMESTAMP
        )
    pin = request.sources[0]
    wrong_hash = pin.model_copy(update={"snapshot_sha256": "0" * 64})
    with pytest.raises(AnalyticsError, match="snapshot changed"):
        regime.evaluate_regime_model(request.model_copy(update={"sources": (wrong_hash,)}), db=db)
    for update in [
        {"code": "VIX", "adjustment": "volatility_index", "return_semantics": "volatility_index"},
        {"provider": "unsupported"},
    ]:
        incompatible = pin.model_copy(update={"identity": pin.identity.model_copy(update=update)})
        with pytest.raises(ValidationError):
            regime.evaluate_regime_model(
                request.model_copy(update={"sources": (incompatible,)}), db=db
            )


def test_empty_history_and_execution_limits(tmp_path, monkeypatch):
    db = tmp_path / "limits.sqlite"
    _seed(db, [100] * 2001)
    _record_quality_scan(db, status="completed")
    request = _regime_request(db, end=_day(2000))
    with pytest.raises(AnalyticsError, match="2,000|2000"):
        regime.evaluate_regime_model(request, db=db)
    empty = tmp_path / "empty.sqlite"
    _seed(empty, [])
    _record_quality_scan(empty, status="completed")
    with pytest.raises(AnalyticsError, match="No local stored observations"):
        _evaluate(empty)
    request = _regime_request(db, end=_day(2))
    execution = REGIME_DEFINITION.metadata.execution.model_copy(update={"max_result_bytes": 128})
    metadata = REGIME_DEFINITION.metadata.model_copy(update={"execution": execution})
    monkeypatch.setattr(
        regime,
        "get_model_definition",
        lambda _: REGIME_DEFINITION.model_copy(update={"metadata": metadata}),
    )
    with pytest.raises(AnalyticsError, match="output size limit"):
        regime.evaluate_regime_model(request, db=db)
