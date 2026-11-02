"""Offline numerical and source-provenance contracts for Issue #30.

Fixtures use fabricated daily observations and never request vendor data.
"""

import math
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.analytics import (
    CALCULATION_VERSION,
    AnalyticsError,
    SeriesIdentity,
    compare_series,
    compute_series,
    validated_observations,
)
from finintel.market_data.analytics_store import run_analytics
from finintel.storage.db import _write_connection


def index_identity(code="SP500"):
    return SeriesIdentity(
        "index", code, "fred", "price_index", "price_index", "daily", "index_points"
    )


def points(*levels):
    dates = [f"2026-10-{10 + i:02d}" for i in range(len(levels))]
    return validated_observations(list(zip(dates, map(str, levels), strict=True)))


def _seed_index(path):
    conn = _write_connection(path)
    with conn:
        for code, levels in (
            ("SP500", ("100", "110", "99", "121", "121", "118", "124")),
            ("DJIA", ("200", "210", "209", "215", "220", "221", "222")),
            ("VIX", ("20", "25", "22", "19", "20", "21", "18")),
        ):
            conn.executemany(
                "INSERT INTO index_daily_closes"
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "VALUES (?,'fred',?,?,'2026-10-29T00:00:00+00:00',?)",
                [
                    (
                        code,
                        f"2026-10-{10 + i:02d}",
                        value,
                        f"https://fred.stlouisfed.org/series/{code}",
                    )
                    for i, value in enumerate(levels)
                ],
            )
    conn.close()


def test_exact_simple_log_cumulative_drawdown_recovery_and_warmup():
    result = compute_series(index_identity(), points(100, 110, 99, 121, 121, 118, 124), window=5)
    p = result["points"]
    assert result["calculation_version"] == CALCULATION_VERSION
    assert p[0]["simple_return"] is None
    assert Decimal(p[1]["simple_return"]) == Decimal("0.1")
    assert float(p[1]["log_return"]) == pytest.approx(math.log(1.1), abs=1e-14)
    assert Decimal(p[2]["drawdown"]) == Decimal("-0.1")
    assert Decimal(p[3]["normalized_100"]) == Decimal(121)
    assert Decimal(p[6]["cumulative_return"]) == Decimal("0.24")
    assert Decimal(result["max_drawdown"]) == Decimal("-0.1")
    assert result["longest_underwater_observed_sessions"] == 1
    assert all(p[i]["rolling_volatility_annualized"] is None for i in range(5))
    assert p[5]["rolling_volatility_annualized"] is not None
    assert p[4]["moving_average"] == "110.2"
    assert p[5]["rsi"] is not None
    assert p[5]["momentum"] is not None
    assert result["period_returns"]["week"]
    assert not result["period_returns"]["month"]


def test_no_data_single_level_and_flat_rsi():
    assert compute_series(index_identity(), [], window=5)["points"] == []
    solo = compute_series(index_identity(), points(100), window=5)
    assert solo["points"][0]["simple_return"] is None
    flat = compute_series(index_identity(), points(100, 100, 100, 100, 100, 100), window=5)
    assert Decimal(flat["points"][-1]["rsi"]) == 50
    assert float(flat["points"][-1]["rolling_volatility_annualized"]) == 0
    assert flat["max_drawdown"] == "0"


def test_vix_level_only_without_explicit_opt_in():
    vix = SeriesIdentity(
        "index", "VIX", "fred", "volatility_index", "volatility_index", "daily", "index_points"
    )
    raw = compute_series(vix, points(20, 30, 40, 25, 20, 21), window=5)
    assert raw["metrics_enabled"] is False
    assert all(row["simple_return"] is None and row["drawdown"] is None for row in raw["points"])
    assert raw["points"][-1]["moving_average"] == "27.2"
    requested = compute_series(
        vix,
        points(20, 30, 40, 25, 20, 21),
        window=5,
        allow_volatility_returns=True,
    )
    assert requested["metrics_enabled"] is True
    with pytest.raises(AnalyticsError, match="volatility"):
        compare_series(vix, points(20, 30), index_identity(), points(10, 20))


def test_common_sessions_only_rejects_strict_misalignment_and_constant_beta():
    left = validated_observations(
        [
            ("2026-10-10", "100"),
            ("2026-10-11", "105"),
            ("2026-10-13", "110"),
            ("2026-10-14", "120"),
            ("2026-10-15", "126"),
            ("2026-10-16", "128"),
        ]
    )
    right = validated_observations(
        [
            ("2026-10-10", "200"),
            ("2026-10-12", "190"),
            ("2026-10-13", "210"),
            ("2026-10-14", "215"),
            ("2026-10-15", "220"),
            ("2026-10-16", "230"),
        ]
    )
    result = compare_series(index_identity(), left, index_identity("DJIA"), right, window=5)
    assert result["overlap_sessions"] == 5
    assert result["left_only_sessions"] == result["right_only_sessions"] == 1
    assert all(v["rolling_correlation"] is None for v in result["points"])  # warm-up
    assert result["points"][1]["session_date"] == "2026-10-13"
    assert Decimal(result["points"][0]["left_normalized_100"]) == 100
    with pytest.raises(AnalyticsError, match="identical observed"):
        compare_series(
            index_identity(), left, index_identity("DJIA"), right, missing_policy="strict"
        )


def test_rolling_correlation_beta_tracking_error_against_independent_reference():
    left = points(100, 104, 106, 111, 109, 114, 121, 120)
    right = points(100, 102, 103, 106, 105, 107, 111, 110)
    result = compare_series(index_identity(), left, index_identity("DJIA"), right, window=5)
    last = result["points"][-1]
    expected_l = [float(left[i].close / left[i - 1].close - 1) for i in range(3, 8)]
    expected_r = [float(right[i].close / right[i - 1].close - 1) for i in range(3, 8)]
    avg_l, avg_r = sum(expected_l) / 5, sum(expected_r) / 5
    covariance = (
        sum((a - avg_l) * (b - avg_r) for a, b in zip(expected_l, expected_r, strict=True)) / 4
    )
    var_r = sum((b - avg_r) ** 2 for b in expected_r) / 4
    assert float(last["rolling_covariance"]) == pytest.approx(covariance, abs=1e-14)
    assert float(last["rolling_beta"]) == pytest.approx(covariance / var_r, rel=1e-12)
    assert -1 <= float(last["rolling_correlation"]) <= 1.00000001
    assert float(last["rolling_tracking_error_annualized"]) >= 0


@pytest.mark.parametrize(
    "levels",
    [
        [("2026-10-10", "100"), ("2026-10-10", "101")],
        [("2026-10-11", "101"), ("2026-10-10", "100")],
        [("2026-10-10", "0")],
        [("2026-10-10", "-12")],
        [("2026-10-10", "NaN")],
    ],
)
def test_bad_or_revised_input_rows_rejected(levels):
    with pytest.raises(AnalyticsError):
        validated_observations(levels)


def test_read_only_api_quality_gate_source_snapshot_and_cross_index(tmp_path, monkeypatch):
    db = tmp_path / "index.sqlite3"
    _seed_index(db)
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    start, end = date(2026, 10, 10), date(2026, 10, 16)
    with pytest.raises(AnalyticsError, match="complete quality scan"):
        run_analytics(start=start, end=end, db=db)
    result = run_analytics(
        start=start,
        end=end,
        window=5,
        comparison_code="DJIA",
        quality_policy="allow_unverified",
        db=db,
    )
    assert result["identity"]["code"] == "SP500"
    assert result["source"]["snapshot_sha256"]
    assert result["comparison"]["overlap_sessions"] == 7
    assert result["unverified_data"] is True
    assert result["read_only"] and result["no_network"]
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/analytics/series",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "window": 5,
                "comparison_code": "DJIA",
                "quality_policy": "allow_unverified",
            },
        )
        denied = client.get(
            "/api/v1/analytics/series",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["points"][0]["normalized_100"] == "100"
    assert payload["comparison"]["overlap_sessions"] == 7
    assert denied.status_code == 422
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_daily_closes").fetchone()[0] == 21
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15


def test_no_database_creation_or_implicit_provider_calls(tmp_path, monkeypatch):
    absent = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(absent))
    with TestClient(app) as client:
        result = client.get(
            "/api/v1/analytics/series",
            params={
                "start": "2026-10-10",
                "end": "2026-10-16",
                "quality_policy": "allow_unverified",
            },
        )
    assert result.status_code == 422
    assert not absent.exists()


def test_source_revision_hash_changes_without_mutating_history(tmp_path):
    db = tmp_path / "index.sqlite3"
    _seed_index(db)
    args = {
        "start": date(2026, 10, 10),
        "end": date(2026, 10, 16),
        "quality_policy": "allow_unverified",
        "db": db,
    }
    first = run_analytics(**args)
    same = run_analytics(**args)
    assert same["points"] == first["points"]
    assert same["source"]["snapshot_sha256"] == first["source"]["snapshot_sha256"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='111',"
            "retrieved_at='2026-10-30T00:00:00+00:00' "
            "WHERE index_code='SP500' AND session_date='2026-10-11'"
        )
    second = run_analytics(**args)
    assert first["source"]["snapshot_sha256"] != second["source"]["snapshot_sha256"]
    assert second["points"][1]["close"] == "111"


def test_vix_api_requires_opt_in_and_rejects_incompatible_benchmark(tmp_path):
    db = tmp_path / "index.sqlite3"
    _seed_index(db)
    args = {
        "start": date(2026, 10, 10),
        "end": date(2026, 10, 16),
        "code": "VIX",
        "quality_policy": "allow_unverified",
        "db": db,
    }
    levels = run_analytics(**args)
    assert levels["metrics_enabled"] is False
    assert all(p["simple_return"] is None for p in levels["points"])
    returns = run_analytics(**args, allow_volatility_returns=True)
    assert returns["metrics_enabled"] is True
    with pytest.raises(AnalyticsError, match="VIX/volatility"):
        run_analytics(**args, comparison_code="SP500")


def test_range_and_provider_unknown_rejected(tmp_path):
    db = tmp_path / "index.sqlite3"
    _seed_index(db)
    with pytest.raises(AnalyticsError, match="date range"):
        run_analytics(start=date(2026, 10, 16), end=date(2026, 10, 10), db=db)
    with pytest.raises(AnalyticsError, match="Unsupported stored"):
        run_analytics(
            start=date(2026, 10, 10),
            end=date(2026, 10, 16),
            provider="another_provider",
            db=db,
        )
