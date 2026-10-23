"""Offline synthetic tests for Alpaca history, safety, pagination and provider isolation."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.alpaca import AlpacaDailyProvider
from finintel.market_data.provider import (
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    parse_alpha_vantage_daily,
)
from finintel.storage.db import list_instruments, read_daily

ALPHA_FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
ALPHA_SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM"
HEADER = {"x-finintel-client": "local-workstation"}
API = "/api/v1/market-data/acquisition"
BARS = [
    {"t": "2026-10-19T04:00:00Z", "o": 100.0, "h": 105.0, "l": 99.0, "c": 103.0, "v": 1500},
    {"t": "2026-10-20T04:00:00Z", "o": 103.0, "h": 109.0, "l": 102.0, "c": 107.0, "v": 1700},
]


def _configure(tmp_path, monkeypatch):
    path = tmp_path / "finintel.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    monkeypatch.setenv("FININTEL_ALPACA_API_KEY_ID", "SYNTHETIC_ALPACA_ID")
    monkeypatch.setenv("FININTEL_ALPACA_API_SECRET_KEY", "SYNTHETIC_ALPACA_SECRET")
    return path


def test_alpaca_provider_two_pages_with_header_credentials(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    called = []

    def respond(request: httpx.Request):
        called.append(request)
        assert request.headers["APCA-API-KEY-ID"] == "SYNTHETIC_ALPACA_ID"
        assert request.headers["APCA-API-SECRET-KEY"] == "SYNTHETIC_ALPACA_SECRET"
        assert request.url.path == "/v2/stocks/bars"
        assert request.url.params["symbols"] == "IBM"
        assert request.url.params["feed"] == "sip"
        assert request.url.params["adjustment"] == "raw"
        token = request.url.params.get("page_token")
        return httpx.Response(
            200,
            json={
                "bars": {"IBM": [BARS[1]] if token else [BARS[0]]},
                "next_page_token": None if token else "PAGE_2",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = AlpacaDailyProvider(client=client).fetch_daily("IBM", period="10y")
    assert len(called) == 2
    assert len(result.observations) == 2
    assert result.provider == "alpaca_sip"
    assert "SYNTHETIC_ALPACA" not in result.source_url
    assert result.observations[-1].close == 107
    assert result.observations[-1].volume == 1700
    assert result.source_url.startswith("https://data.alpaca.markets/v2/stocks/bars?")


def test_alpaca_exits_on_quota_and_corrupt_pagination(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429))) as client,
        pytest.raises(ProviderRateLimited),
    ):
        AlpacaDailyProvider(client=client).fetch_daily("IBM")

    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"bars": {"IBM": [BARS[0]]}, "next_page_token": "REPEAT"}
                )
            )
        ) as client,
        pytest.raises(ProviderPayloadInvalid),
    ):
        AlpacaDailyProvider(client=client).fetch_daily("IBM")


def test_alpaca_rejects_unauthorized_credentials_and_invalid_bars(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json={"error": "bad"})
            )
        ) as client,
        pytest.raises(ProviderError, match="entitlement"),
    ):
        AlpacaDailyProvider(client=client).fetch_daily("IBM")
    broken = {**BARS[0], "h": 90}
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"bars": {"IBM": [broken]}, "next_page_token": None}
                )
            )
        ) as client,
        pytest.raises(ProviderPayloadInvalid),
    ):
        AlpacaDailyProvider(client=client).fetch_daily("IBM")


def test_alpaca_and_alpha_prices_coexist_and_are_queryable(tmp_path, monkeypatch):
    db = _configure(tmp_path, monkeypatch)

    def respond(request: httpx.Request):
        return httpx.Response(200, json={"bars": {"IBM": BARS}, "next_page_token": None})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        batch = AlpacaDailyProvider(client=client).fetch_daily("IBM")

    class FakeProvider:
        def fetch_daily(self, symbol, *, period="10y"):
            assert symbol == "IBM" and period == "5y"
            return batch

    monkeypatch.setattr("finintel.market_data.acquisition.AlpacaDailyProvider", FakeProvider)
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "SYNTHETIC_ALPHA")
    alpha = parse_alpha_vantage_daily(
        ALPHA_FIXTURE.read_bytes(), symbol="IBM", source_url=ALPHA_SOURCE
    )

    class FakeAlpha:
        def fetch_daily(self, symbol, *, outputsize="compact"):
            return alpha

    monkeypatch.setattr("finintel.market_data.acquisition.AlphaVantageDailyProvider", FakeAlpha)
    with TestClient(app) as client:
        alpha_response = client.post(API, headers=HEADER, json={"symbol": "IBM"})
        alpaca_response = client.post(
            API, headers=HEADER, json={"symbol": "IBM", "provider": "alpaca_sip", "period": "5y"}
        )
        alpha_daily = client.get("/api/v1/market-data/IBM/daily?provider=alpha_vantage")
        alpaca_daily = client.get("/api/v1/market-data/IBM/daily?provider=alpaca_sip")
        symbols = client.get("/api/v1/market-data/instruments")
        status = client.get(API + "/status")

    assert alpha_response.status_code == 200, alpha_response.text
    assert alpaca_response.status_code == 200, alpaca_response.text
    assert alpaca_response.json()["provider"] == "alpaca_sip"
    assert alpaca_response.json()["inserted"] == 2
    assert alpaca_daily.json()["observations"][0]["close"] == "103.0"
    assert alpha_daily.json()["observations"][0]["close"] == "98.50"
    assert {item["provider"] for item in symbols.json()} == {"alpha_vantage", "alpaca_sip"}
    assert "SYNTHETIC_ALPACA_SECRET" not in json.dumps(status.json())
    assert status.json()["alpaca_configured"] is True
    assert len(list_instruments(db)) == 2
    assert len(read_daily("IBM", provider="alpaca_sip", db=db)["observations"]) == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 5


def test_no_key_or_invalid_provider_never_fetches(tmp_path, monkeypatch):
    db = tmp_path / "empty.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.delenv("FININTEL_ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("FININTEL_ALPACA_API_SECRET_KEY", raising=False)
    with TestClient(app) as client:
        missing = client.post(API, headers=HEADER, json={"provider": "alpaca_sip", "symbol": "IBM"})
        invalid = client.post(API, headers=HEADER, json={"provider": "yahoo", "symbol": "IBM"})
        bad_period = client.post(
            API, headers=HEADER, json={"provider": "alpaca_sip", "symbol": "IBM", "period": "200y"}
        )
    assert missing.status_code == 503
    assert invalid.status_code == 422
    assert bad_period.status_code == 422
    assert not db.exists()
