"""Tests for bounded SQLite read benchmarks."""

import sqlite3

from finintel.market_data.performance import benchmark


def test_benchmark_is_read_only_and_bounded(tmp_path):
    db = tmp_path / "prices.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE series (id INTEGER PRIMARY KEY, symbol TEXT, provider TEXT);
        CREATE TABLE daily_prices (
            series_id INTEGER, session_date TEXT, open TEXT, high TEXT,
            low TEXT, close TEXT, volume INTEGER,
            PRIMARY KEY (series_id, session_date)
        );
        INSERT INTO series VALUES (1, 'SPY', 'alpaca_sip');
        INSERT INTO daily_prices VALUES (1, '2026-10-20', '100', '110', '90', '105', 1000);
        """
    )
    conn.close()
    report = benchmark(db)
    assert report["series_count"] == 1
    assert report["sample_count"] == 1
    assert report["sample_queries"][0]["rows"] == 1
    assert "directory_under_2_seconds" in report["acceptance"]
    assert report["acceptance"]["uses_price_index"]
    assert report["thresholds"]["sample_price_query_seconds"] == 0.25
    assert report["performance_query_plan"]
    assert "not a full-table benchmark" in report["scope"]
