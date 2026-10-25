"""Coverage diagnostics regression tests."""

import sqlite3

from finintel.market_data.diagnostics import _weekdays_between, diagnostics


def test_weekday_gap_counts():
    from datetime import date

    assert _weekdays_between(date(2026, 10, 23), date(2026, 10, 26)) == 0
    assert _weekdays_between(date(2026, 10, 19), date(2026, 10, 22)) == 2


def test_diagnostics_coverage_and_provider_comparison(tmp_path):
    db = tmp_path / "prices.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE series (
          id INTEGER PRIMARY KEY, symbol TEXT, provider TEXT, adjustment TEXT
        );
        CREATE TABLE daily_prices (
          series_id INTEGER, session_date TEXT, close TEXT,
          PRIMARY KEY (series_id, session_date)
        );
        INSERT INTO series VALUES
          (1, 'AAPL', 'alpaca_sip', 'raw'),
          (2, 'AAPL', 'tiingo', 'raw'),
          (3, 'EMPTY', 'alpaca_sip', 'raw');
        INSERT INTO daily_prices VALUES
          (1, '2026-10-19', '100'),
          (1, '2026-10-22', '200'),
          (2, '2026-10-19', '102'),
          (2, '2026-10-22', '200');
        """
    )
    conn.close()
    report = diagnostics(db)
    assert report["series_without_prices"] == 1
    assert report["cross_provider_pairs_compared"] == 1
    assert report["cross_provider_comparisons"][0]["overlap_sessions"] == 2
    assert report["cross_provider_comparisons"][0]["close_divergence_over_1pct"] == 1
    assert report["weekday_gap_candidates"] == 2
    assert len(report["corporate_action_jump_candidates"]) == 1
