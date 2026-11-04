"""Point-in-time recovery-duration reference fixtures for Issue #30."""

from finintel.market_data.analytics import (
    SeriesIdentity,
    compute_series,
    validated_observations,
)


IDENTITY = SeriesIdentity(
    "index", "SP500", "fred", "price_index", "price_index", "daily", "index_points"
)


def test_recovery_episodes_preserve_actual_sessions_and_censored_tail():
    records = validated_observations(
        [
            ("2026-10-02", "100"),
            ("2026-10-05", "110"),
            ("2026-10-06", "99"),
            ("2026-10-08", "105"),
            ("2026-10-09", "110"),
            ("2026-10-12", "115"),
            ("2026-10-13", "92"),
            ("2026-10-15", "100"),
        ]
    )
    full = compute_series(IDENTITY, records, window=5)
    assert full["max_drawdown"] == "-0.2"
    assert full["longest_completed_recovery_observed_sessions"] == 3
    assert full["ongoing_drawdown_observed_sessions"] == 2
    assert full["drawdown_episodes"] == [
        {
            "status": "recovered",
            "peak_session_date": "2026-10-05",
            "trough_session_date": "2026-10-06",
            "recovery_session_date": "2026-10-09",
            "trough_drawdown": "-0.1",
            "underwater_observed_sessions": 2,
            "peak_to_recovery_observed_sessions": 3,
            "trough_to_recovery_observed_sessions": 2,
        },
        {
            "status": "ongoing",
            "peak_session_date": "2026-10-12",
            "trough_session_date": "2026-10-13",
            "recovery_session_date": None,
            "trough_drawdown": "-0.2",
            "underwater_observed_sessions": 2,
            "peak_to_recovery_observed_sessions": None,
            "trough_to_recovery_observed_sessions": None,
        },
    ]
    truncated = compute_series(IDENTITY, records[:4], window=5)
    assert truncated["drawdown_episodes"][0]["status"] == "ongoing"
    assert truncated["drawdown_episodes"][0]["recovery_session_date"] is None
    assert truncated["longest_completed_recovery_observed_sessions"] is None
    assert full == compute_series(IDENTITY, records, window=5)


def test_equal_peak_recovery_flat_and_volatility_level_gate():
    flat = compute_series(
        IDENTITY,
        validated_observations(
            [("2026-10-01", "100"), ("2026-10-02", "100"), ("2026-10-03", "100")]
        ),
        window=5,
    )
    assert flat["drawdown_episodes"] == []
    assert flat["longest_completed_recovery_observed_sessions"] is None
    assert flat["ongoing_drawdown_observed_sessions"] is None
    records = validated_observations(
        [
            ("2026-10-01", "100"),
            ("2026-10-02", "90"),
            ("2026-10-03", "100"),
            ("2026-10-04", "99"),
            ("2026-10-05", "101"),
        ]
    )
    result = compute_series(IDENTITY, records, window=5)
    assert [e["peak_to_recovery_observed_sessions"] for e in result["drawdown_episodes"]] == [
        2,
        2,
    ]
    vix = SeriesIdentity(
        "index", "VIX", "fred", "volatility_index", "volatility_index", "daily", "index_points"
    )
    assert compute_series(vix, records, window=5)["drawdown_episodes"] == []
    enabled = compute_series(vix, records, window=5, allow_volatility_returns=True)
    assert len(enabled["drawdown_episodes"]) == 2
