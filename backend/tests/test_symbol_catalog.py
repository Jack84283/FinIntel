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
    SCHEMA_V2,
    import_snapshot,
    list_instruments,
    lookup_symbol_type,
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
    assert path.exists()  # The catalog is durably seeded on first search.
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0] >= 73
        assert (
            conn.execute("SELECT asset_type FROM symbol_catalog WHERE symbol='^GSPC'").fetchone()[0]
            == "Index"
        )


def test_migrate_v1_prices_without_data_loss(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    assert search_symbols(db=db)["source"] == "starter"
    assert replace_symbol_catalog(parse_listing_csv(LISTING), db=db)["total"] == 12
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 0


def test_upgrading_populated_v1_preserves_imported_prices(tmp_path, monkeypatch):
    """Existing PR #4 user databases must keep prices and their import history."""
    from finintel.market_data.provider import parse_alpha_vantage_daily

    db = tmp_path / "existing-v1.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    batch = parse_alpha_vantage_daily(FIXTURE.read_bytes(), symbol="IBM", source_url=SOURCE)
    snapshot = tmp_path / "legacy-IBM.json"
    snapshot.write_text(batch.model_dump_json())
    import_snapshot(snapshot, db=db)
    with sqlite3.connect(db) as conn:
        before_prices = conn.execute("SELECT * FROM daily_prices").fetchall()
        before_runs = conn.execute("SELECT * FROM ingestion_runs").fetchall()
        before_series = conn.execute("SELECT * FROM series").fetchall()
        conn.execute("DROP TABLE symbol_catalog")
        conn.execute("DROP TABLE action_events")
        conn.execute("DROP TABLE index_memberships")
        conn.execute("PRAGMA user_version = 1")
    assert search_symbols(db=db)["source"] == "starter"
    assert replace_symbol_catalog(parse_listing_csv(LISTING), db=db)["total"] == 12
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT * FROM daily_prices").fetchall() == before_prices
        assert conn.execute("SELECT * FROM ingestion_runs").fetchall() == before_runs
        assert conn.execute("SELECT * FROM series").fetchall() == before_series


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
    assert response.json()["total"] == 12
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
    assert directory.json()["total"] == 12
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



def test_listing_skips_isolated_invalid_metadata_without_losing_valid_rows():
    # A real-world listing may contain an empty or malformed name in one row.
    feed = (
        LISTING
        + b"BAD,Name\\tWithTab,NYSE,Stock,2020-01-01,null,Active\\n"
        + b"EMPTY,,NASDAQ,Stock,2020-01-01,null,Active\\n"
    )
    rows = parse_listing_csv(feed)
    assert [row["symbol"] for row in rows] == ["AAPL", "SPY"]


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


def test_migration_of_provider_synced_v2_preserves_listing_and_index_types(tmp_path):
    """Databases already synced by earlier PR #6 drafts must upgrade in place."""
    db = tmp_path / "existing-v2.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_V1 + "\n" + SCHEMA_V2)
        conn.execute(
            "INSERT INTO symbol_catalog(symbol, name, asset_type, exchange, source, updated_at) "
            "VALUES ('AAPL', 'Apple Incorporated', 'Stock', 'NASDAQ', "
            "'alpha_vantage', '2026-10-21T12:00:00Z')"
        )
        conn.execute("PRAGMA user_version = 2")
    results = search_symbols(query="AAPL", db=db)
    assert results["source"] == "alpha_vantage"
    assert results["results"][0]["name"] == "Apple Incorporated"
    assert (
        search_symbols(query="^GSPC", asset_type="Index", db=db)["results"][0]["asset_type"]
        == "Index"
    )
    assert (
        search_symbols(query="FXAIX", asset_type="Mutual Fund", db=db)["results"][0]["asset_type"]
        == "Mutual Fund"
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0] == 11


def test_sync_preserves_indexes_and_mutual_funds(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    search_symbols(db=db)  # Seed durable references.
    synced = replace_symbol_catalog(parse_listing_csv(LISTING), db=db)
    assert synced["total"] == 12
    indexes = search_symbols(asset_type="Index", db=db)
    funds = search_symbols(asset_type="Mutual Fund", db=db)
    assert len(indexes["results"]) == 6
    assert len(funds["results"]) == 4
    assert indexes["results"][0]["symbol"].startswith("^")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 0


def test_lookup_rejects_unsupported_seeded_fund_on_unmigrated_v2(tmp_path):
    db = tmp_path / "preview-v2.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_V1 + "\n" + SCHEMA_V2)
        conn.execute("PRAGMA user_version = 2")
    assert lookup_symbol_type("FXAIX", db=db) == "Mutual Fund"
    assert lookup_symbol_type("^GSPC", db=db) == "Index"
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
