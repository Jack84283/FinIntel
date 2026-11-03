"""Issue #31 multi-index: one local snapshot, true common dates, no synthetic filling."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_basket import run_index_basket
from finintel.storage.db import _write_connection


def _seed(path):
    conn = _write_connection(path)
    with conn:
        for code, excluded, first in (
            ("SP500", set(), 100),
            ("DJIA", {"2026-10-11"}, 200),
            ("NASDAQCOM", {"2026-10-12"}, 300),
        ):
            conn.executemany(
                "INSERT INTO index_daily_closes "
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "VALUES (?,'fred',?,?,'2026-11-01T00:00:00+00:00',?)",
                [
                    (
                        code,
                        f"2026-10-{day:02d}",
                        str(first + (day - 10) * (10 if code == "SP500" else 5)),
                        f"https://fred.stlouisfed.org/series/{code}",
                    )
                    for day in range(10, 20)
                    if f"2026-10-{day:02d}" not in excluded
                ],
            )
    conn.close()


def _run(db, **overrides):
    kwargs = {
        "codes": ["SP500", "DJIA", "NASDAQCOM"],
        "start": date(2026, 10, 10),
        "end": date(2026, 10, 19),
        "window": 5,
        "quality_policy": "allow_unverified",
        "db": db,
    }
    return run_index_basket(**(kwargs | overrides))


def test_basket_recomputes_returns_from_only_shared_dates(tmp_path):
    db = tmp_path / "basket.sqlite3"
    _seed(db)
    before = db.stat().st_size
    first = _run(db)
    second = _run(db)
    assert first == second
    assert first["codes"] == ["SP500", "DJIA", "NASDAQCOM"]
    assert first["common_sessions"] == 8
    assert first["first_common_session"] == "2026-10-10"
    assert first["last_common_session"] == "2026-10-19"
    assert [item["session_date"] for item in first["points"]] == [
        "2026-10-10",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
        "2026-10-17",
        "2026-10-18",
        "2026-10-19",
    ]
    assert [item["excluded_nonshared_sessions"] for item in first["series"]] == [2, 1, 1]
    assert all(len(item["source"]["snapshot_sha256"]) == 64 for item in first["series"])
    baseline = first["points"][0]["values"]
    assert baseline["SP500"]["normalized_100"] == "100"
    assert baseline["DJIA"]["normalized_100"] == "100"
    assert baseline["NASDAQCOM"]["normalized_100"] == "100"
    assert baseline["SP500"]["relative_strength"] is None
    assert baseline["DJIA"]["relative_strength"] == "1"
    assert first["points"][1]["values"]["SP500"]["simple_return"] == "0.3"
    assert first["points"][0]["values"]["DJIA"]["rolling_correlation"] is None
    assert first["points"][5]["values"]["DJIA"]["rolling_correlation"] is not None
    assert first["quality_policy"] == "allow_unverified"
    assert first["unverified_data"] is True
    assert first["alignment"] == "intersection"
    assert first["read_only"] is first["no_network"] is first["no_synthetic_sessions"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_daily_closes").fetchone()[0] == 28
    assert db.stat().st_size == before


def test_rejects_vix_duplicate_unsupported_and_unverified_default(tmp_path):
    db = tmp_path / "basket.sqlite3"
    _seed(db)
    for codes in (
        ["SP500", "SP500"],
        ["SP500", "VIX"],
        ["VIX", "SP500"],
        ["SP500", "UNKNOWN"],
        ["SP500"],
        ["SP500", "DJIA", "NDX", "VIX", "NASDAQCOM"],
    ):
        with pytest.raises(AnalyticsError):
            _run(db, codes=codes)
    with pytest.raises(AnalyticsError, match="Current complete Data Quality scan"):
        _run(db, quality_policy="require_complete")
    with pytest.raises(AnalyticsError, match="future"):
        _run(db, end=date(2030, 1, 1))


def test_no_common_sessions_fails_closed_without_filling(tmp_path):
    db = tmp_path / "basket.sqlite3"
    _seed(db)
    with pytest.raises(AnalyticsError, match="two shared"):
        _run(db, start=date(2026, 10, 11), end=date(2026, 10, 12))


def test_api_uses_local_db_and_enforces_bounded_types(tmp_path, monkeypatch):
    db = tmp_path / "basket.sqlite3"
    _seed(db)
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/analytics/basket",
            params={
                "codes": "SP500,DJIA,NASDAQCOM",
                "start": "2026-10-10",
                "end": "2026-10-19",
                "window": 5,
                "quality_policy": "allow_unverified",
            },
        )
        rejected = client.get(
            "/api/v1/analytics/basket",
            params={
                "codes": "SP500,VIX",
                "start": "2026-10-10",
                "end": "2026-10-19",
                "quality_policy": "allow_unverified",
            },
        )
        gated = client.get(
            "/api/v1/analytics/basket",
            params={
                "codes": "SP500,DJIA",
                "start": "2026-10-10",
                "end": "2026-10-19",
            },
        )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["common_sessions"] == 8
    assert len(value["points"]) == 8
    assert Decimal(value["points"][1]["values"]["DJIA"]["normalized_100"]) > 100
    assert rejected.status_code == 422
    assert gated.status_code == 422
