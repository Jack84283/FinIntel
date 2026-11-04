"""Snapshot-only index membership trend contracts (Issue #31)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.membership import load_membership
from finintel.market_data.membership_trend import constituent_snapshot_trend


def _seed(db):
    load_membership(
        "SP500",
        "2026-10-01",
        "verified-oct01",
        ["AAPL", "MSFT", "NVDA"],
        expected_count=3,
        source_kind="verified_public",
        db=db,
    )
    load_membership(
        "SP500",
        "2026-10-06",
        "verified-oct06",
        ["AAPL", "GOOG", "NVDA"],
        expected_count=3,
        source_kind="verified_public",
        db=db,
    )
    load_membership(
        "SP500",
        "2026-10-10",
        "etf-holdings-proxy",
        ["AAPL", "GOOG"],
        expected_count=2,
        source_kind="fund_derived_proxy",
        db=db,
    )
    load_membership(
        "SP500",
        "2026-10-20",
        "incomplete-oct20",
        ["AAPL"],
        source_expected_count=3,
        source_kind="verified_public",
        db=db,
    )


def test_observed_dates_counts_and_sourced_confidence_no_interpolation(tmp_path):
    db = tmp_path / "trend.sqlite"
    _seed(db)
    size_before = db.stat().st_size
    actual = constituent_snapshot_trend(
        "SP500", from_as_of="2026-10-01", to_as_of="2026-10-21", db=db
    )
    assert actual["total_snapshots"] == 4
    assert actual["snapshot_only"] is actual["no_interpolation"] is actual["no_lookahead"] is True
    assert actual["read_only"]
    points = actual["snapshots"]
    assert [p["snapshot_as_of"] for p in points] == [
        "2026-10-01",
        "2026-10-06",
        "2026-10-10",
        "2026-10-20",
    ]
    assert [p["member_count"] for p in points] == [3, 3, 2, 1]
    assert points[0]["added_members"] is None and points[0]["removed_members"] is None
    assert points[0]["change_status"] == "first_observed"
    assert (points[1]["added_members"], points[1]["removed_members"]) == (1, 1)
    assert points[1]["change_status"] == "verified_pair"
    assert (points[2]["added_members"], points[2]["removed_members"]) == (0, 1)
    assert points[2]["evidence_status"] == "proxy_only"
    assert points[2]["change_status"] == "indicative_pair"
    assert points[3]["evidence_status"] == "incomplete"
    assert points[3]["change_status"] == "indicative_pair"
    assert db.stat().st_size == size_before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_memberships").fetchone()[0] == 9


def test_selection_range_and_paging_use_adjacent_stored_snapshot_not_future(tmp_path):
    db = tmp_path / "trend.sqlite"
    _seed(db)
    first = constituent_snapshot_trend(
        "SP500",
        from_as_of="2026-10-06",
        to_as_of="2026-10-20",
        limit=1,
        offset=0,
        db=db,
    )
    assert first["total_snapshots"] == 3
    assert first["snapshots"][0]["snapshot_as_of"] == "2026-10-06"
    assert first["snapshots"][0]["previous_snapshot_as_of"] is None
    assert first["snapshots"][0]["added_members"] is None

    second = constituent_snapshot_trend(
        "SP500",
        from_as_of="2026-10-06",
        to_as_of="2026-10-20",
        limit=1,
        offset=1,
        db=db,
    )
    assert second["snapshots"][0]["previous_snapshot_as_of"] == "2026-10-06"
    assert second["snapshots"][0]["snapshot_as_of"] == "2026-10-10"
    assert (second["snapshots"][0]["added_members"], second["snapshots"][0]["removed_members"]) == (
        0,
        1,
    )
    before_future = constituent_snapshot_trend(
        "SP500",
        from_as_of="2026-10-01",
        to_as_of="2026-10-08",
        db=db,
    )
    assert [r["snapshot_as_of"] for r in before_future["snapshots"]] == ["2026-10-01", "2026-10-06"]
    assert (
        constituent_snapshot_trend("SP500", from_as_of="2026-09-01", to_as_of="2026-09-30", db=db)[
            "snapshots"
        ]
        == []
    )


def test_source_date_mismatch_is_not_verified(tmp_path):
    db = tmp_path / "trend.sqlite"
    _seed(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE index_membership_snapshots SET source_as_of='2026-09-30' "
            "WHERE index_code='SP500' AND as_of='2026-10-06'"
        )
    result = constituent_snapshot_trend(
        "SP500", from_as_of="2026-10-01", to_as_of="2026-10-10", db=db
    )
    assert result["snapshots"][1]["source_as_of"] == "2026-09-30"
    assert result["snapshots"][1]["evidence_status"] == "source_date_mismatch"
    assert result["snapshots"][1]["change_status"] == "indicative_pair"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "VIX", "from_as_of": "2026-10-01", "to_as_of": "2026-10-20"},
        {"code": "SP500", "from_as_of": "2026-10-21", "to_as_of": "2026-10-20"},
        {"code": "SP500", "from_as_of": "2026-10-01", "to_as_of": "2026-12-01"},
        {"code": "SP500", "from_as_of": "2026-10-01", "to_as_of": "2026-10-20", "limit": 101},
        {"code": "SP500", "from_as_of": "2026-10-01", "to_as_of": "2026-10-20", "offset": -1},
    ],
)
def test_invalid_dates_codes_and_bounds_fail_closed(tmp_path, kwargs):
    db = tmp_path / "absent.sqlite"
    with pytest.raises(ValueError):
        constituent_snapshot_trend(**kwargs, db=db)
    assert not db.exists()


def test_api_does_not_create_absent_db_and_rejects_invalid_queries(tmp_path, monkeypatch):
    db = tmp_path / "trend.sqlite"
    _seed(db)
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    params = {
        "code": "SP500",
        "from_as_of": "2026-10-01",
        "to_as_of": "2026-10-20",
        "limit": 2,
        "offset": 1,
    }
    with TestClient(app) as client:
        response = client.get("/api/v1/market-data/indexes/snapshot-trend", params=params)
        invalid = client.get(
            "/api/v1/market-data/indexes/snapshot-trend",
            params={**params, "code": "VIX"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_snapshots"] == 4 and len(body["snapshots"]) == 2
    assert body["snapshots"][0]["previous_snapshot_as_of"] == "2026-10-01"
    assert invalid.status_code == 422

    absent = tmp_path / "missing.sqlite"
    monkeypatch.setenv("FININTEL_DB_PATH", str(absent))
    with TestClient(app) as client:
        missing = client.get("/api/v1/market-data/indexes/snapshot-trend", params=params)
    assert missing.status_code == 200
    assert missing.json()["total_snapshots"] == 0
    assert missing.json()["snapshots"] == []
    assert not absent.exists()
