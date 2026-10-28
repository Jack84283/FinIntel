"""Reference corporate events are read-only, explicit and mocked in tests."""

import json
import sqlite3

import httpx

from finintel.market_data.corporate_reference import verify_events


def test_tiingo_split_and_dividend_reference(monkeypatch, tmp_path):
    monkeypatch.setenv("FININTEL_TIINGO_API_KEY", "test-only-key")
    db = tmp_path / "local.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE series (
            id INTEGER PRIMARY KEY, symbol TEXT, provider TEXT, adjustment TEXT
        );
        CREATE TABLE daily_prices (series_id INTEGER, session_date TEXT, close TEXT);
        INSERT INTO series VALUES (1, 'AAPL', 'alpaca_sip', 'raw_unadjusted');
        INSERT INTO daily_prices VALUES (1, '2026-10-19', '200');
        INSERT INTO daily_prices VALUES (1, '2026-10-20', '100');
        """
    )
    conn.commit()
    conn.close()

    def handler(request):
        assert request.headers["Authorization"] == "Token test-only-key"
        payload = [
            {"date": "2026-10-20T00:00:00+00:00", "splitFactor": 2, "divCash": 0},
            {"date": "2026-10-21T00:00:00+00:00", "splitFactor": 1, "divCash": 0.25},
        ]
        return httpx.Response(200, content=json.dumps(payload).encode())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_events(db, "AAPL", client=client)
    assert result["event_count"] == 2
    assert result["events"][0]["check"] == "split_ratio_consistent"
    assert result["events"][1]["check"] == "dividend_reference_only"
    assert result["read_only"]
