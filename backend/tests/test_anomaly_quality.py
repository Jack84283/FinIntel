"""Unit coverage for conservative, configurable anomaly rules."""

from decimal import Decimal

from finintel.market_data.anomaly_quality import (
    ANOMALY_RULE_VERSION,
    AnomalyThresholds,
    invalid_ohlcv_fields,
    normalize_thresholds,
    parse_timestamp,
    price_jump_candidate,
    thresholds_identity,
    volume_spike_candidate,
    zero_volume_candidate,
)


def test_thresholds_are_documented_and_normalized():
    thresholds = normalize_thresholds(
        {
            "price_jump_ratio": "1.5",
            "volume_spike_minimum": "5000",
            "stale_after_days": 45,
        }
    )

    assert thresholds == AnomalyThresholds(
        price_jump_ratio=Decimal("1.5"), volume_spike_minimum=5000, stale_after_days=45
    )
    assert thresholds_identity(thresholds)["rule_version"] == ANOMALY_RULE_VERSION
    assert thresholds_identity(thresholds)["price_jump_ratio"] == "1.5"


def test_price_jump_marks_raw_unadjusted_series_for_corporate_action_review():
    raw = price_jump_candidate("100", "250", adjustment="raw_unadjusted")
    adjusted = price_jump_candidate("100", "250", adjustment="adjusted")

    assert raw is not None and adjusted is not None
    assert raw["evidence"]["possible_corporate_action"] is True
    assert raw["evidence"]["corporate_action_status"] == "not_checked"
    assert adjusted["evidence"]["possible_corporate_action"] is False
    assert raw["evidence"]["candidate_only"] is True


def test_volume_spike_requires_liquid_baseline():
    assert volume_spike_candidate(20_000, [10, 11, 9]) is None
    finding = volume_spike_candidate(20_000, [1_000, 1_100, 900])

    assert finding is not None
    assert finding["rule"] == "volume_spike_candidate"
    assert finding["evidence"]["baseline_volume"] == "1000"


def test_zero_volume_requires_meaningful_price_move_and_baseline():
    assert zero_volume_candidate("100", "101", 0, [10, 11, 9]) is None
    finding = zero_volume_candidate("100", "120", 0, [1_000, 1_100, 900])

    assert finding is not None
    assert finding["rule"] == "suspicious_zero_volume"
    assert finding["evidence"]["volume"] == 0


def test_impossible_ohlcv_fields_are_explicit():
    assert invalid_ohlcv_fields({"open": "-1", "high": "2", "low": "1", "close": "2"}, 10) == [
        "open"
    ]
    assert invalid_ohlcv_fields({"open": "5", "high": "4", "low": "1", "close": "2"}, -1) == [
        "open_order",
        "volume",
    ]


def test_timestamps_without_timezone_are_treated_as_utc():
    parsed = parse_timestamp("2026-11-01T12:00:00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-11-01T12:00:00+00:00"
