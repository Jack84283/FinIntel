"""Tests for the read-only market data audit."""

import sqlite3

from finintel.market_data.validation import validate


def test_validation_reports_ohlcv_and_empty_series(tmp_path):
    db = tmp_path / "prices.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE series (
            id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, provider TEXT NOT NULL
        );
        CREATE TABLE daily_prices (
            series_id INTEGER NOT NULL, session_date TEXT NOT NULL,
            open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
            close TEXT NOT NULL, volume INTEGER NOT NULL
        );
        INSERT INTO series VALUES (1, 'AAPL', 'alpaca_sip'), (2, 'MSFT', 'tiingo');
        INSERT INTO daily_prices VALUES
            (1, '2026-10-20', '10', '12', '9', '11', 1200),
            (1, '2026-10-21', '13', '12', '9', '11', 500);
        """
    )
    conn.close()
    result = validate(db)
    assert result["series_count"] == 2
    assert result["series_without_prices"] == 1
    assert result["price_rows"] == 2
    assert result["issues"]["invalid_ohlc_range"] == 1
    assert result["provider_series"] == {"alpaca_sip": 1, "tiingo": 1}


def test_validation_handles_empty_database(tmp_path):
    db = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE series (id INTEGER, symbol TEXT, provider TEXT)")
    conn.execute("CREATE TABLE daily_prices (series_id INTEGER, session_date TEXT, open TEXT, high TEXT, low TEXT, close TEXT, volume INTEGER)")
    conn.close()
    result = validate(db)
    assert result["price_rows"] == 0
    assert result["issues"] == {}
