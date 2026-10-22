"""Integrated market acquisition never uses the network in CI."""

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.provider import (
    ProviderPayloadInvalid,
    ProviderRateLimited,
    parse_alpha_vantage_daily,
)
from finintel.storage.db import list_imports, read_daily

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM"
API = "/api/v1/market-data/acquisition"
HEADERS = {"x-finintel-client": "local-workstation"}
JSON_HEADERS = {**HEADERS, "content-type": "application/json"}


def _configure(tmp_path, monkeypatch, *, provider_error=None):
    db = tmp_path / "finintel.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_ALPHA_VANTAGE_API_KEY", "SYNTHETIC_TEST_KEY")
    calls = []
    batch = parse_alpha_vantage_daily(FIXTURE.read_bytes(), symbol="IBM", source_url=SOURCE)

    class FakeProvider:
        def fetch_daily(self, symbol, *, outputsize="compact"):
            calls.append((symbol, outputsize))
            if provider_error is not None:
                raise provider_error
            return batch

    monkeypatch.setattr("finintel.market_data.acquisition.AlphaVantageDailyProvider", FakeProvider)
    return db, calls


def test_fetch_validate_persist_and_read_without_manual_import(tmp_path, monkeypatch):
    db, calls = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        status = client.get(API + "/status")
        result = client.post(API, headers=HEADERS, json={"symbol": "ibm"})
        instruments = client.get("/api/v1/market-data/instruments")
        daily = client.get("/api/v1/market-data/IBM/daily?latest=true")
        imports = client.get("/api/v1/market-data/imports")

    assert status.status_code == 200
    assert status.json()["key_configured"] is True
    assert "SYNTHETIC_TEST_KEY" not in json.dumps(status.json())
    assert result.status_code == 200
    summary = result.json()
    assert summary["status"] == "stored"
    assert summary["symbol"] == "IBM"
    assert summary["inserted"] == 3
    assert summary["observations"] == 3
    assert summary["not_live_quotes"] is True
    assert calls == [("IBM", "compact")]
    assert instruments.json()[0]["sessions"] == 3
    assert daily.json()["observations"][0]["close"] == "98.50"
    assert imports.json()[0]["inserted"] == 3
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 3
    # Temp staging snapshots are deleted, no long-term staging artifacts.
    assert sorted(item.name for item in tmp_path.iterdir()) == [db.name]


def test_repeat_fetch_does_not_duplicate_price_rows(tmp_path, monkeypatch):
    db, calls = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        first = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
        second = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
    assert first.json()["inserted"] == 3
    assert second.json()["inserted"] == 0
    assert second.json()["unchanged"] == 3
    assert len(list_imports(db=db)) == 2
    assert len(read_daily("IBM", db=db)["observations"]) == 3
    assert len(calls) == 2  # each explicit click consumes a provider request


def test_provider_failure_preserves_existing_good_sqlite_data(tmp_path, monkeypatch):
    db, _ = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post(API, headers=HEADERS, json={"symbol": "IBM"}).status_code == 200
    baseline = read_daily("IBM", db=db)["observations"]
    runs_before = len(list_imports(db=db))

    _configure(tmp_path, monkeypatch, provider_error=ProviderPayloadInvalid("Invalid data"))
    with TestClient(app) as client:
        failure = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
    assert failure.status_code == 502
    assert len(list_imports(db=db)) == runs_before
    assert read_daily("IBM", db=db)["observations"] == baseline


def test_quota_is_not_retried_and_does_not_create_db(tmp_path, monkeypatch):
    db, calls = _configure(
        tmp_path, monkeypatch, provider_error=ProviderRateLimited("Daily provider quota reached")
    )
    with TestClient(app) as client:
        response = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
    assert response.status_code == 429
    assert calls == [("IBM", "compact")]
    assert not db.exists()


def test_missing_key_blocks_provider_and_returns_safe_status(tmp_path, monkeypatch):
    db, calls = _configure(tmp_path, monkeypatch)
    monkeypatch.delenv("FININTEL_ALPHA_VANTAGE_API_KEY")
    with TestClient(app) as client:
        status = client.get(API + "/status")
        result = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
    assert status.json()["key_configured"] is False
    assert result.status_code == 503
    assert "FININTEL_ALPHA_VANTAGE_API_KEY" in result.json()["detail"]
    assert not db.exists()
    assert calls == []


def test_bad_request_csrf_and_invalid_entitlements_do_not_fetch(tmp_path, monkeypatch):
    db, calls = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post(API, json={"symbol": "IBM"}).status_code == 403
        assert (
            client.post(
                API,
                headers={**HEADERS, "origin": "https://untrusted.example"},
                json={"symbol": "IBM"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                API,
                headers={**HEADERS, "origin": "http://127.0.0.1:1420"},
                json={"symbol": "IBM", "outputsize": "invalid"},
            ).status_code
            == 422
        )
        assert client.post(API, headers=HEADERS, json={"symbol": "../abc"}).status_code == 422
        assert (
            client.post(API, headers=HEADERS, json={"symbol": "IBM", "apikey": "BAD"}).status_code
            == 422
        )
        assert (
            client.post(
                API,
                headers={"x-finintel-client": "local-workstation", "content-type": "text/plain"},
                content='{"symbol":"IBM"}',
            ).status_code
            == 422
        )
    assert calls == []
    assert not db.exists()


def test_concurrent_acquisition_is_blocked(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    from finintel.storage.api import _ACQUIRE_LOCK

    assert _ACQUIRE_LOCK.acquire(blocking=False)
    try:
        with TestClient(app) as client:
            response = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
        assert response.status_code == 409
    finally:
        _ACQUIRE_LOCK.release()


def test_explicit_premium_full_option_is_forwarded(tmp_path, monkeypatch):
    _, calls = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        result = client.post(API, headers=HEADERS, json={"symbol": "IBM", "outputsize": "full"})
    assert result.status_code == 200
    assert calls == [("IBM", "full")]


def test_provider_errors_do_not_expose_user_key(tmp_path, monkeypatch):
    _, _ = _configure(tmp_path, monkeypatch, provider_error=ProviderRateLimited("Quota reached"))
    with TestClient(app) as client:
        response = client.post(API, headers=HEADERS, json={"symbol": "IBM"})
    assert "SYNTHETIC_TEST_KEY" not in response.text



def test_index_and_mutual_fund_are_not_sent_to_stock_price_provider(tmp_path, monkeypatch):
    db, calls = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        index = client.post(API, headers=HEADERS, json={"symbol": "^GSPC"})
        mutual = client.post(API, headers=HEADERS, json={"symbol": "FXAIX"})
    assert index.status_code == 422
    assert mutual.status_code == 422
    assert "Mutual Fund history is not supported" in mutual.json()["detail"]
    assert calls == []
    assert not db.exists()
