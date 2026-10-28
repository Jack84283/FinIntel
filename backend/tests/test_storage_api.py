"""Local API read routes; temporary databases, never the real user's storage."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.provider import parse_alpha_vantage_daily
from finintel.storage.db import import_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&outputsize=compact&datatype=json"


def _seed(tmp_path, monkeypatch):
    db = tmp_path / "market.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    batch = parse_alpha_vantage_daily(FIXTURE.read_bytes(), symbol="IBM", source_url=SOURCE)
    snap = tmp_path / "IBM.json"
    snap.write_text(batch.model_dump_json())
    import_snapshot(snap, db=db)
    return db


def test_api_uninitialized_storage_is_empty(tmp_path, monkeypatch):
    db = tmp_path / "absent.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    with TestClient(app) as client:
        assert client.get("/api/v1/market-data/instruments").json() == []
        assert client.get("/api/v1/market-data/imports").json() == []
        assert client.get("/api/v1/market-data/IBM/daily").status_code == 404
    assert not db.exists()


def test_api_catalog_prices_and_ingestion_history(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        catalog = client.get("/api/v1/market-data/instruments")
        directory = client.get("/api/v1/market-data/instruments/directory")
        daily = client.get("/api/v1/market-data/ibm/daily?limit=2")
        history = client.get("/api/v1/market-data/imports")

    assert directory.status_code == 200
    assert directory.json()[0]["symbol"] == "IBM"
    assert directory.json()[0]["sessions"] == 0
    assert catalog.status_code == 200
    assert catalog.json()[0]["symbol"] == "IBM"
    assert catalog.json()[0]["sessions"] == 3
    assert daily.status_code == 200
    data = daily.json()
    assert data["price_adjustment"] == "raw_unadjusted"
    assert data["data_kind"] == "historical_eod"
    assert data["has_more"] is True
    assert data["observations"][0]["open"] == "98.25"
    assert data["observations"][0]["session_date"] == "2026-10-16"
    assert len(data["observations"]) == 2
    assert history.status_code == 200
    assert history.json()[0]["inserted"] == 3
    assert "apikey" not in str(history.json())


def test_api_date_filters_and_bad_inputs(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        result = client.get(
            "/api/v1/market-data/IBM/daily?start_date=2026-10-19&end_date=2026-10-20"
        )
        assert result.status_code == 200
        assert len(result.json()["observations"]) == 2
        assert client.get("/api/v1/market-data/IBM/daily?limit=5000").status_code == 422
        assert client.get("/api/v1/market-data/IBM/daily?limit=-1").status_code == 422
        assert client.get("/api/v1/market-data/IBM/daily?start_date=nope").status_code == 422
        assert (
            client.get(
                "/api/v1/market-data/IBM/daily?start_date=2026-10-21&end_date=2026-10-01"
            ).status_code
            == 422
        )
        assert client.get("/api/v1/market-data/INVALID!!!!/daily").status_code == 422
        assert client.get("/api/v1/market-data/MISS/daily").status_code == 404


def test_unsupported_schema_reports_503(tmp_path, monkeypatch):
    db = tmp_path / "future.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    with TestClient(app) as client:
        assert client.get("/api/v1/market-data/instruments").status_code == 503
        assert client.get("/api/v1/market-data/imports").status_code == 503


def test_no_network_or_write_routes(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/v1/market-data/IBM/daily").status_code == 405
        assert client.post("/api/v1/market-data/imports").status_code == 405
        assert client.get("/api/v1/market-data/IBM/daily").status_code == 200


def test_latest_limit_returns_newest_but_chronological(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        result = client.get("/api/v1/market-data/IBM/daily?latest=true&limit=2")
    assert result.status_code == 200
    data = result.json()
    assert data["has_more"] is True
    assert [row["session_date"] for row in data["observations"]] == ["2026-10-19", "2026-10-20"]


def test_ten_year_query_limit_is_accepted(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/market-data/IBM/daily?latest=true&limit=4000&start_date=2016-10-22"
        )
    assert response.status_code == 200
    assert len(response.json()["observations"]) == 3
    assert response.json()["has_more"] is False


def test_full_import_history_search_and_pagination(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO symbol_catalog "
            "(symbol, name, asset_type, exchange, source, updated_at) "
            "VALUES ('IBM', 'International Business Machines', 'Stock', 'NYSE', 'starter', '2026-10-20')"
        )
        # Import-log rows are append-only. Repeat the fixture's run to exercise
        # pages beyond the previous 100-record UI cap.
        conn.execute(
            "INSERT INTO ingestion_runs (series_id, retrieved_at, imported_at, payload_sha256, "
            "provider_terms_url, first_session, last_session, observations, inserted, updated, unchanged, stale, source_url) "
            "SELECT series_id, retrieved_at, imported_at, payload_sha256, provider_terms_url, first_session, "
            "last_session, observations, inserted, updated, unchanged, stale, source_url "
            "FROM ingestion_runs LIMIT 1"
        )
    with TestClient(app) as client:
        first = client.get("/api/v1/market-data/imports/search?limit=1&offset=0")
        second = client.get("/api/v1/market-data/imports/search?limit=1&offset=1")
        named = client.get("/api/v1/market-data/imports/search?q=Business%20Machines")
        absent = client.get("/api/v1/market-data/imports/search?q=not-a-company")
        wrong_provider = client.get("/api/v1/market-data/imports/search?provider=tiingo")
        bad = client.get("/api/v1/market-data/imports/search?limit=101")
    assert first.status_code == second.status_code == named.status_code == 200
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == len(second.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    assert named.json()["total"] == 2
    assert absent.json()["total"] == wrong_provider.json()["total"] == 0
    assert bad.status_code == 422


def test_equibles_stock_catalog_source_serializes(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO symbol_catalog "
            "(symbol, name, asset_type, exchange, source, updated_at) "
            "VALUES ('ZXEQ', 'Equibles Example', 'Stock', '', 'equibles_proxy', '2026-10-23')"
        )
    with TestClient(app) as client:
        response = client.get("/api/v1/market-data/symbols?q=ZXEQ")
    assert response.status_code == 200
    assert response.json()["source"] == "equibles_proxy"
    assert response.json()["results"][0]["symbol"] == "ZXEQ"
