"""Point-in-time index membership regression tests using local synthetic snapshots."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.membership import (
    index_constituents,
    list_indexes,
    load_membership,
    memberships,
    symbol_index_memberships,
)
from finintel.storage.db import _write_connection


def test_multiple_indexes_and_as_of_removals(tmp_path):
    db = tmp_path / "indices.sqlite"
    load_membership("SP500", "2026-10-01", "source-sp500-01", ["AAPL", "MSFT"], db=db)
    load_membership("NDX", "2026-10-01", "source-ndx-01", ["AAPL", "GOOG"], db=db)
    load_membership("DJIA", "2026-10-01", "source-djia-01", ["AAPL"], db=db)
    load_membership("RUT", "2026-10-01", "source-rut-01", ["PLTR"], db=db)
    earlier = symbol_index_memberships("AAPL", as_of="2026-10-01", db=db)
    assert {x["code"] for x in earlier["indexes"] if x["is_member"] is True} == {
        "SP500",
        "NDX",
        "DJIA",
    }
    load_membership("SP500", "2026-10-15", "source-sp500-15", ["MSFT"], db=db)
    now = symbol_index_memberships("AAPL", as_of="2026-10-23", db=db)
    flags = {x["code"]: x["is_member"] for x in now["indexes"]}
    assert flags == {"SP500": False, "DJIA": True, "NDX": True, "RUT": False}
    assert {x["index_code"] for x in memberships("AAPL", as_of="2026-10-23", db=db)} == {
        "DJIA",
        "NDX",
    }
    assert (
        symbol_index_memberships("AAPL", as_of="2026-09-01", db=db)["indexes"][0]["is_member"]
        is None
    )
    old_snapshot = index_constituents("SP500", as_of="2026-10-10", db=db)
    assert old_snapshot["snapshot_as_of"] == "2026-10-01"
    assert [x["symbol"] for x in old_snapshot["items"]] == ["AAPL", "MSFT"]
    latest = index_constituents("SP500", as_of="2026-10-23", db=db)
    assert latest["source"] == "source-sp500-15"
    assert [x["symbol"] for x in latest["items"]] == ["MSFT"]


def test_multiple_index_badges_and_unknown_symbols(tmp_path):
    db = tmp_path / "idx.sqlite"
    load_membership("SP500", "2026-10-01", "sample-sp", ["AAPL", "MSFT"], db=db)
    load_membership("NDX", "2026-10-01", "sample-ndx", ["AAPL"], db=db)
    result = index_constituents("SP500", as_of="2026-10-20", query="AAPL", db=db)
    assert result["total"] == 1
    assert result["items"][0]["member_indexes"] == ["NDX", "SP500"]
    assert result["items"][0]["name"] == "Apple"
    empty = symbol_index_memberships("UNKNOWN", db=db)
    assert all(x["is_member"] is None or x["is_member"] is False for x in empty["indexes"])
    missing = index_constituents("RUT", as_of="2026-10-20", db=db)
    assert missing["snapshot_as_of"] is None
    assert missing["snapshot_members"] is None


def test_search_pagination_and_literal_wildcards(tmp_path):
    db = tmp_path / "idx.sqlite"
    load_membership("SP500", "2026-10-01", "demo", ["AAPL", "MSFT", "META"], db=db)
    page = index_constituents("SP500", query="M", offset=1, limit=1, db=db)
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert index_constituents("SP500", query="%", db=db)["total"] == 0
    assert index_constituents("SP500", query="_", db=db)["total"] == 0
    with pytest.raises(ValueError):
        index_constituents("INVALID", db=db)


def test_invalid_source_duplicate_symbol_and_known_etf_are_rejected(tmp_path):
    db = tmp_path / "idx.sqlite"
    load_membership("SP500", "2026-10-01", "good", ["AAPL"], db=db)
    with pytest.raises(ValueError, match="unique"):
        load_membership("SP500", "2026-10-01", "bad", ["AAPL", "aapl"], db=db)
    with pytest.raises(ValueError, match="expected"):
        load_membership("SP500", "2026-10-01", "bad", ["AAPL"], db=db, expected_count=500)
    with pytest.raises(ValueError, match="source"):
        load_membership("SP500", "2026-10-01", "bad\nnewlines", ["MSFT"], db=db)
    with pytest.raises(ValueError, match="non-stock"):
        load_membership("SP500", "2026-10-01", "wrong", ["SPY"], db=db)
    assert [row["symbol"] for row in index_constituents("SP500", db=db)["items"]] == ["AAPL"]


def test_v5_migration_preserves_old_index_snapshots(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = _write_connection(db)
    conn.executescript(
        """
        DROP TABLE index_daily_closes;
        DROP TABLE index_memberships;
        CREATE TABLE index_memberships (
            index_code TEXT NOT NULL CHECK(index_code IN ('SP500','DJIA')),
            symbol TEXT NOT NULL, as_of TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(index_code, symbol, as_of)
        );
        INSERT INTO index_memberships VALUES ('SP500','AAPL','2026-10-01','original');
        PRAGMA user_version=5;
        """
    )
    conn.close()
    load_membership("NDX", "2026-10-02", "added", ["AAPL"], db=db)
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 11
        assert check.execute("SELECT COUNT(*) FROM index_memberships").fetchone()[0] == 2
    assert {x["index_code"] for x in memberships("AAPL", db=db)} == {"SP500", "NDX"}


def test_absent_database_read_only_and_api_metadata(tmp_path, monkeypatch):
    db = tmp_path / "absent.sqlite"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    catalog = list_indexes(db=db)
    assert len(catalog) == 4
    assert all(x["status"] == "not_imported" for x in catalog)
    with TestClient(app) as client:
        result = client.get("/api/v1/market-data/indexes")
        assert result.status_code == 200
        assert all(x["members"] is None for x in result.json())
        page = client.get("/api/v1/market-data/indexes/SP500/constituents")
        assert page.status_code == 200
        assert page.json()["items"] == []
        assert client.get("/api/v1/market-data/indexes/FAKE/constituents").status_code == 422
        assert (
            client.get("/api/v1/market-data/indexes/SP500/constituents?limit=999").status_code
            == 422
        )
    assert not db.exists()


def test_read_api_date_filter_avoids_lookahead_and_does_not_write(tmp_path, monkeypatch):
    db = tmp_path / "point.sqlite"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    load_membership("SP500", "2026-10-15", "newer", ["NVDA"], db=db)
    bytes_before = db.read_bytes()
    with TestClient(app) as client:
        before = client.get("/api/v1/market-data/indexes?as_of=2026-10-14")
        assert before.json()[0]["snapshot_as_of"] is None
        after = client.get("/api/v1/market-data/indexes/SP500/constituents?as_of=2026-10-23")
        assert after.json()["items"][0]["symbol"] == "NVDA"
        stock = client.get("/api/v1/market-data/indexes/symbol/NVDA/memberships?as_of=2026-10-14")
        assert stock.status_code == 200
        assert stock.json()["indexes"][0]["is_member"] is None
    assert db.read_bytes() == bytes_before
