"""Offline index-level history acceptance tests; never fetch live FRED during CI."""

import sqlite3
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.index_history import (
    MAX_ROWS,
    IndexHistoryError,
    acquire_index_closes,
    fetch_fred_index,
    index_catalog,
    parse_fred_csv,
    read_index_closes,
    store_index_closes,
)
from finintel.storage.backup import create_backup, restore_backup
from finintel.storage.db import _write_connection


def csv_bytes(code="SP500", close="6000.25"):
    return (
        "observation_date," + code + "\n"
        "2026-10-21,5998.50\n"
        "2026-10-22,.\n"
        "2026-10-23," + close + "\n"
    ).encode()


def test_parse_close_only_fred_series():
    assert parse_fred_csv("SP500", csv_bytes()) == [
        ("2026-10-21", "5998.50"),
        ("2026-10-23", "6000.25"),
    ]
    assert parse_fred_csv("VIX", csv_bytes("VIXCLS"))[-1][1] == "6000.25"


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>not a CSV</html>",
        b"observation_date,SP500\n2026-10-21,abc\n",
        b"observation_date,SP500\n2026-10-23,42\n2026-10-21,50\n",
        b"observation_date,SP500\n2026-10-21,1\n2026-10-21,1\n",
        b"observation_date,SP500\n2026-10-21,-5\n",
    ],
)
def test_rejects_bad_provider_content_before_write(payload):
    with pytest.raises(IndexHistoryError):
        parse_fred_csv("SP500", payload)


def test_fred_row_limit_is_exact():
    start = date(1950, 1, 1)
    rows = [f"{start + timedelta(days=index)},{index + 1}" for index in range(MAX_ROWS)]
    payload = ("observation_date,SP500\n" + "\n".join(rows) + "\n").encode()
    assert len(parse_fred_csv("SP500", payload)) == MAX_ROWS

    too_many = rows + [f"{start + timedelta(days=MAX_ROWS)},{MAX_ROWS + 1}"]
    oversized = ("observation_date,SP500\n" + "\n".join(too_many) + "\n").encode()
    with pytest.raises(IndexHistoryError):
        parse_fred_csv("SP500", oversized)


def test_mocked_source_fetch_uses_fixed_host_and_series():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fred.stlouisfed.org"
        assert request.url.params.get("id") == "SP500"
        return httpx.Response(200, content=csv_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_fred_index("SP500", client=client)
    assert len(rows) == 2


def test_import_idempotency_and_exact_provenance(tmp_path):
    db = tmp_path / "index.sqlite3"
    original = parse_fred_csv("SP500", csv_bytes())
    first = store_index_closes("SP500", original, db=db)
    second = store_index_closes("SP500", original, db=db)
    changed = parse_fred_csv("SP500", csv_bytes(close="6012.50"))
    revised = store_index_closes("SP500", changed, db=db)

    assert first["inserted"] == 2
    assert second["unchanged"] == 2
    assert revised["updated"] == 1
    assert first["source_url"] == "https://fred.stlouisfed.org/series/SP500"
    assert read_index_closes("SP500", db=db)["observations"][-1]["close"] == "6012.50"
    assert index_catalog(db=db)[0]["sessions"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 0


def test_no_database_created_by_read_only_queries(tmp_path):
    db = tmp_path / "absent.sqlite3"
    assert index_catalog(db=db)[0]["sessions"] == 0
    assert read_index_closes("SP500", db=db)["observations"] == []
    assert not db.exists()


def test_acquire_and_restore_preserves_index_series(tmp_path):
    db = tmp_path / "index.sqlite3"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=csv_bytes("NASDAQ100"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = acquire_index_closes("NDX", client=client, db=db)
    assert result["inserted"] == 2
    backup = create_backup(db=db)
    store_index_closes("SP500", parse_fred_csv("SP500", csv_bytes()), db=db)
    restore_backup(backup["id"], "merge", db=db)
    assert read_index_closes("SP500", db=db)["observations"]
    assert len(read_index_closes("NDX", db=db)["observations"]) == 2
    restore_backup(backup["id"], "replace", db=db)
    assert not read_index_closes("SP500", db=db)["observations"]
    assert len(read_index_closes("NDX", db=db)["observations"]) == 2


def test_version_six_migrates_without_erasing_equity_data(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    conn = _write_connection(db)
    with conn:
        conn.execute("DROP TABLE index_daily_closes")
        conn.execute("PRAGMA user_version=6")
        conn.execute("INSERT INTO instruments VALUES ('AAPL','2026-01-01')")
    conn.close()
    result = store_index_closes("VIX", [("2026-10-23", "21.25")], db=db)
    assert result["inserted"] == 1
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 10
        assert check.execute("SELECT symbol FROM instruments").fetchone()[0] == "AAPL"
    assert list((db.parent / "backups").glob("finintel-pre-restore-*.sqlite3"))


def test_api_requires_desktop_auth_and_retains_read_only_get(tmp_path, monkeypatch):
    db = tmp_path / "api.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "a" * 32)
    with TestClient(app) as client:
        without = client.post("/api/v1/market-data/index-history/SP500/acquire")
        catalog = client.get(
            "/api/v1/market-data/index-history", headers={"x-finintel-process-token": "a" * 32}
        )
        empty = client.get(
            "/api/v1/market-data/index-history/SP500/daily",
            headers={"x-finintel-process-token": "a" * 32},
        )
    assert without.status_code == 403
    assert catalog.status_code == 200
    assert catalog.json()[0]["code"] == "SP500"
    assert empty.json()["observations"] == []
    assert not db.exists()
