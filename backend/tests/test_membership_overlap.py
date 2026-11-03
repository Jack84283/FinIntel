"""Offline evidence-based overlap regression fixtures for Issue #31."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.membership import load_membership
from finintel.market_data.membership_overlap import constituent_overlap


def _load(db):
    load_membership(
        "SP500", "2026-10-01", "verified-sp-01",
        ["AAPL", "MSFT", "NVDA"], expected_count=3,
        source_kind="verified_public", db=db,
    )
    load_membership(
        "NDX", "2026-10-01", "verified-ndx-01",
        ["AAPL", "NVDA", "GOOG"], expected_count=3,
        source_kind="verified_public", db=db,
    )


def test_verified_exact_dated_snapshot_and_paginated_overlap(tmp_path):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    before = db.stat().st_size
    result = constituent_overlap("SP500", "NDX", as_of="2026-10-01", db=db)
    assert result["status"] == "verified_snapshot"
    assert result["common_members"] == 2
    assert result["union_members"] == 4
    assert result["jaccard_ratio"] == "0.5"
    assert result["filtered_total"] == 2
    assert [row["symbol"] for row in result["items"]] == ["AAPL", "NVDA"]
    assert result["sources"][0]["snapshot_as_of"] == "2026-10-01"
    assert result["sources"][1]["snapshot_as_of"] == "2026-10-01"
    assert result["no_lookahead"] and result["read_only"] and result["snapshot_only"]
    page = constituent_overlap("SP500", "NDX", as_of="2026-10-01", limit=1, offset=1, db=db)
    assert page["common_members"] == 2
    assert page["filtered_total"] == 2
    assert [x["symbol"] for x in page["items"]] == ["NVDA"]
    literal = constituent_overlap("SP500", "NDX", as_of="2026-10-01", query="%", db=db)
    assert literal["filtered_total"] == 0
    assert literal["common_members"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_memberships").fetchone()[0] == 6
    assert db.stat().st_size == before


def test_older_complete_snapshot_is_not_verified_for_future_requested_date(tmp_path):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    current = constituent_overlap("SP500", "NDX", as_of="2026-10-20", db=db)
    assert current["status"] == "earlier_snapshot"
    assert current["common_members"] == 2
    assert current["requested_as_of"] == "2026-10-20"
    assert all(s["snapshot_as_of"] == "2026-10-01" for s in current["sources"])
    # Future membership cannot be projected backwards into a dated request.
    load_membership(
        "NDX", "2026-10-25", "future-ndx", ["MSFT"],
        expected_count=1, source_kind="verified_public", db=db,
    )
    earlier = constituent_overlap("SP500", "NDX", as_of="2026-10-20", db=db)
    assert earlier["common_members"] == 2
    assert earlier["sources"][1]["source"] == "verified-ndx-01"
    later = constituent_overlap("SP500", "NDX", as_of="2026-10-25", db=db)
    assert later["status"] == "earlier_snapshot"
    assert later["common_members"] == 1
    assert later["sources"][1]["snapshot_as_of"] == "2026-10-25"


def test_proxy_and_incomplete_snapshots_never_claim_official_membership(tmp_path):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    load_membership(
        "NDX", "2026-10-15", "ETF holdings-derived proxy",
        ["AAPL", "GOOG"], expected_count=2,
        source_kind="fund_derived_proxy", db=db,
    )
    proxied = constituent_overlap("SP500", "NDX", as_of="2026-10-15", db=db)
    assert proxied["status"] == "proxy_only"
    assert proxied["sources"][1]["source_kind"] == "fund_derived_proxy"
    assert proxied["common_members"] == 1
    # A partial import is not silently treated as complete even with a count.
    load_membership(
        "NDX", "2026-10-18", "incomplete-source",
        ["AAPL", "MSFT"], source_expected_count=100,
        source_kind="verified_public", db=db,
    )
    partial = constituent_overlap("SP500", "NDX", as_of="2026-10-18", db=db)
    assert partial["status"] == "incomplete"
    assert partial["sources"][1]["validation_status"] == "incomplete"
    assert partial["sources"][1]["expected_count"] == 100


def test_missing_snapshot_never_becomes_a_zero_overlap(tmp_path):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    missing = constituent_overlap("SP500", "RUT", as_of="2026-10-20", db=db)
    assert missing["status"] == "unavailable"
    assert missing["common_members"] is None
    assert missing["union_members"] is None
    assert missing["jaccard_ratio"] is None
    assert missing["filtered_total"] is None
    assert missing["sources"][1]["snapshot_as_of"] is None
    earliest = constituent_overlap("SP500", "NDX", as_of="2026-09-20", db=db)
    assert earliest["status"] == "unavailable"
    assert earliest["common_members"] is None
    assert earliest["items"] == []


def test_empty_overlap_is_zero_only_when_both_snapshots_exist(tmp_path):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    load_membership(
        "DJIA", "2026-10-01", "verified-djia",
        ["IBM", "CAT"], expected_count=2,
        source_kind="official_licensed", db=db,
    )
    result = constituent_overlap("SP500", "DJIA", as_of="2026-10-01", db=db)
    assert result["status"] == "verified_snapshot"
    assert result["common_members"] == 0
    assert result["union_members"] == 5
    assert result["jaccard_ratio"] == "0"
    assert result["filtered_total"] == 0


@pytest.mark.parametrize(
    ("left", "right", "when", "query", "limit", "offset"),
    [
        ("SP500", "SP500", "2026-10-01", "", 20, 0),
        ("SP500", "VIX", "2026-10-01", "", 20, 0),
        ("SP500", "NDX", "2026-10-01", "a" * 81, 20, 0),
        ("SP500", "NDX", "2026-10-01", "\n", 20, 0),
        ("SP500", "NDX", "2026-10-01", "", 101, 0),
        ("SP500", "NDX", "2026-10-01", "", 20, -1),
        ("SP500", "NDX", "2026-12-01", "", 20, 0),
    ],
)
def test_invalid_inputs_rejected_without_writes(tmp_path, left, right, when, query, limit, offset):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    with pytest.raises(ValueError):
        constituent_overlap(
            left, right, as_of=when, query=query, limit=limit, offset=offset, db=db
        )


def test_http_api_and_absent_database_no_implicit_creation(tmp_path, monkeypatch):
    db = tmp_path / "overlap.sqlite"
    _load(db)
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    params = {
        "left": "SP500", "right": "NDX", "as_of": "2026-10-01",
        "limit": 1, "offset": 0,
    }
    with TestClient(app) as client:
        ok = client.get("/api/v1/market-data/indexes/overlap", params=params)
        bad = client.get(
            "/api/v1/market-data/indexes/overlap",
            params={**params, "right": "SP500"},
        )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["status"] == "verified_snapshot"
    assert payload["common_members"] == 2
    assert payload["filtered_total"] == 2
    assert len(payload["items"]) == 1
    assert bad.status_code == 422
    absent = tmp_path / "nonexisting.sqlite"
    monkeypatch.setenv("FININTEL_DB_PATH", str(absent))
    with TestClient(app) as client:
        no_data = client.get("/api/v1/market-data/indexes/overlap", params=params)
    assert no_data.status_code == 200
    assert no_data.json()["status"] == "unavailable"
    assert no_data.json()["common_members"] is None
    assert not absent.exists()
