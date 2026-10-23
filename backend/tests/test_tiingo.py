"""Offline Tiingo parsing, provider authentication and persisted source provenance."""
import json

import httpx
import pytest

from finintel.market_data.provider import ProviderPayloadInvalid
from finintel.market_data.tiingo import TiingoDailyProvider, parse_prices
from finintel.storage.db import import_snapshot, read_daily
from finintel.market_data.__main__ import write_snapshot


def test_tiingo_fetch_and_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FININTEL_TIINGO_API_KEY", "test-token")
    def handler(request):
        assert request.headers["Authorization"] == "Token test-token"
        assert request.url.host == "api.tiingo.com"
        return httpx.Response(200, json=[
            {"date": "2026-01-02T00:00:00.000Z", "open": 10, "high": 12,
             "low": 9, "close": 11, "volume": 500},
            {"date": "2026-01-05T00:00:00.000Z", "open": 11, "high": 13,
             "low": 10, "close": 12, "volume": 700},
        ])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = TiingoDailyProvider(client=client).fetch_daily("AAPL", period="10y")
    assert batch.provider == "tiingo"
    snapshot = tmp_path / "tiingo.json"
    write_snapshot(snapshot, batch)
    import_snapshot(snapshot, db=tmp_path / "market.sqlite3")
    stored = read_daily("AAPL", provider="tiingo", db=tmp_path / "market.sqlite3")
    assert stored is not None
    assert len(stored["observations"]) == 2


def test_tiingo_rejects_bad_candles():
    payload = [{"date": "2026-01-02T00:00:00Z", "open": 12, "high": 11,
                "low": 9, "close": 10, "volume": 100}]
    with pytest.raises(ProviderPayloadInvalid):
        parse_prices(json.dumps(payload).encode())
