"""Stock/ETF symbol catalog: offline startup, provider CSV and v1 upgrades."""

import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.provider import ProviderPayloadInvalid, ProviderRateLimited
from finintel.market_data.symbol_catalog import fetch_active_listings, parse_listing_csv
from finintel.storage.db import (
    SCHEMA_V1,
    import_snapshot,
    list_instruments,
    replace_symbol_catalog,
    search_symbols,
)

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM"
API = "/api/v1/market-data/symbols"
HEADERS = {"x-finintel-client": "local-workstation"}
LISTING = (
    b"symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
    b"AAPL,Apple Inc,NASDAQ,Stock,1980-12-12,null,Active\n"
    b"SPY,SPDR S&P 500 ETF,NYSE ARCA,ETF,1993-01-22,null,Active\n"
    b"ZZZ,Old Stock,NYSE,Stock,1990-01-01,2020-01-01,Delisted\n"
)


def test_starter_symbols_readable_without_database_or_key(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    monkeypatch.delenv("FININTEL_ALPHA_VANTAGE_API_KEY", raising=False)
    with TestClient(app) as client:
        both = client.get(API)
        stocks = client.get(API + "?asset_type=Stock&q=apple")
        etfs = client.get(API + "?asset_type=ETF&q=SPY")
    assert both.status_code == 200
    assert both.json()["source"] == "starter"
    assert both.json()["total"] >= 40
    assert stocks.json()["results"][0]["symbol"] == "AAPL"
    assert etfs.json()["results"][0]["symbol"] == "SPY"
    assert not path.exists()  # GET never creates or migrates


def test_migrate_v1_prices_without_data_loss(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    assert search_symbols(db=db)["source"] == "starter"
    assert replace_symbol_catalog(parse_listing_csv(LISTING), db=db)["total"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 0


def test_catalog_sync_does_not_change_historical_prices(tmp_path, monkeypatch):
    db = tmp_path / "financial.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    from finintel.market_data.provider import parse_alpha_vantage_daily

    batch = parse_alpha_vantage_daily(FIXTURE.read_bytes(), symbol="IBM", source_url=SOURCE)
    saved = tmp_path / "IBM.json"
    saved.write_text(batch.model_dump_json())
    import_snapshot(saved, db=db)
    before = list_instruments(db)[0]
    with sqlite3.connect(db) as conn:
        prices_before = conn.execute("SELECT * FROM daily_prices").fetchall()
        import_before = conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]

    mock_rows = parse_listing_csv(LISTING)
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "DUMMY_SECRET")
    monkeypatch.setattr("finintel.storage.api.fetch_active_listings", lambda: mock_rows)
    with TestClient(app) as client:
        response = client.post(API + "/sync", headers=HEADERS)
        after = client.get(API + "?q=AAPL")
        empty = client.get(API + "?asset_type=Stock&q=NO_MATCH")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert after.json()["source"] == "alpha_vantage"
    assert after.json()["results"][0]["symbol"] == "AAPL"
    assert empty.json()["results"] == []
    assert list_instruments(db)[0] == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM daily_prices").fetchall() == prices_before
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == import_before


def test_bad_listing_preserves_last_successful_directory(tmp_path, monkeypatch):
    db = tmp_path / "financial.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "DUMMY_SECRET")
    rows = parse_listing_csv(LISTING)
    replace_symbol_catalog(rows, db=db)

    def invalid_feed():
        raise ProviderPayloadInvalid("Malformed provider listing")

    monkeypatch.setattr("finintel.storage.api.fetch_active_listings", invalid_feed)
    with TestClient(app) as client:
        response = client.post(API + "/sync", headers=HEADERS)
        directory = client.get(API)
    assert response.status_code == 502
    assert directory.json()["total"] == 2
    assert directory.json()["results"][0]["symbol"] == "AAPL"


def test_symbol_sync_rejects_cross_origin_and_missing_key(tmp_path, monkeypatch):
    db = tmp_path / "financial.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "DUMMY_SECRET")
    with TestClient(app) as client:
        assert client.post(API + "/sync").status_code == 403
        assert (
            client.post(
                API + "/sync", headers={**HEADERS, "origin": "https://bad.invalid"}
            ).status_code
            == 403
        )
        assert client.get(API + "?asset_type=Crypto").status_code == 422
        assert client.get(API + "?limit=1000").status_code == 422
    monkeypatch.delenv("FININTEL_ALPHA_VANTAGE_API_KEY")
    with TestClient(app) as client:
        assert client.post(API + "/sync", headers=HEADERS).status_code == 503
    assert not db.exists()


def test_csv_checks_active_type_and_duplicate_conflicts():
    rows = parse_listing_csv(LISTING)
    assert [(row["symbol"], row["asset_type"]) for row in rows] == [
        ("AAPL", "Stock"),
        ("SPY", "ETF"),
    ]
    assert all(row["symbol"] != "ZZZ" for row in rows)
    duplicate = LISTING + b"AAPL,Someone Else,NASDAQ,Stock,2000-01-01,null,Active\n"
    import pytest

    with pytest.raises(ProviderPayloadInvalid):
        parse_listing_csv(duplicate)
    with pytest.raises(ProviderPayloadInvalid):
        parse_listing_csv(b"not,csv\nsomething,wrong\n")
    with pytest.raises(ProviderRateLimited):
        parse_listing_csv(b'{"Note":"quota exceeded"}')


def test_remote_listing_uses_secure_request_without_logging_key(monkeypatch):
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "DUMMY_SECRET")
    captured = []

    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, content=LISTING)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_active_listings(client=client)
    assert len(rows) == 2
    assert len(captured) == 1
    assert captured[0].url.params["function"] == "LISTING_STATUS"
    assert captured[0].url.params["state"] == "active"
    assert captured[0].url.scheme == "https"
    assert captured[0].url.host == "www.alphavantage.co"
