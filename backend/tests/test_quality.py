"""Durable data-quality candidate and review behavior."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.quality import (
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.storage import db as db_module
from finintel.storage.db import _write_connection


def seed_quality_database(path):
    conn = _write_connection(path)
    conn.execute("INSERT INTO instruments VALUES ('AAPL', '2026-01-01')")
    conn.execute("INSERT INTO instruments VALUES ('TSLA', '2026-01-01')")
    series = [
        (1, "AAPL", "alpaca_sip"),
        (2, "AAPL", "tiingo"),
        (3, "TSLA", "alpaca_sip"),
    ]
    for series_id, symbol, provider in series:
        conn.execute(
            "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
            "VALUES (?, ?, ?, '1d', 'raw_unadjusted', 'historical_eod')",
            (series_id, symbol, provider),
        )
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id, series_id, retrieved_at, imported_at, payload_sha256, source_url, "
            "provider_terms_url, first_session, last_session, observations, inserted, updated, unchanged, stale) "
            "VALUES (?, ?, '2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00', ?, ?, ?, '2026-10-19', '2026-10-21', 2, 2, 0, 0, 0)",
            (
                series_id,
                series_id,
                f"hash-{series_id}",
                f"https://example.test/{provider}",
                "https://example.test/terms",
            ),
        )
    rows = [
        (1, "2026-10-19", "99", "101", "98", "100", 10, 1),
        (1, "2026-10-21", "249", "251", "248", "250", 10, 1),
        (2, "2026-10-21", "199", "201", "198", "200", 10, 2),
        # The database layer preserves imported rows; the quality scanner flags
        # this locally invalid candle without rewriting it.
        (3, "2026-10-20", "10", "9", "8", "10", 10, 3),
    ]
    conn.executemany(
        "INSERT INTO daily_prices "
        "(series_id, session_date, open, high, low, close, volume, retrieved_at, ingestion_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-31T00:00:00+00:00', ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO index_daily_closes "
        "(index_code, provider, session_date, close, retrieved_at, source_url) "
        "VALUES ('SP500', 'fred', ?, ?, '2026-10-31T00:00:00+00:00', 'https://fred.stlouisfed.org/series/SP500')",
        [("2026-10-19", "5000"), ("2026-10-21", "10000")],
    )
    conn.commit()
    conn.close()


def test_quality_scan_persists_redacted_candidates_without_changing_prices(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    before = (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id, session_date, open, high, low, close, volume FROM daily_prices ORDER BY series_id"
        )
        .fetchall()
    )

    result = run_quality_scan(db)
    types = {item["finding_type"] for item in result["examples"]}
    assert {"missing_session", "price_anomaly", "ohlcv_anomaly", "provider_divergence"} <= types
    assert result["finding_count"] >= 6
    assert any(item["instrument_kind"] == "index" for item in result["examples"])
    assert all("example.test" not in item["evidence"] for item in result["examples"])
    assert (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id, session_date, open, high, low, close, volume FROM daily_prices ORDER BY series_id"
        )
        .fetchall()
        == before
    )

    stored = list_findings(db, limit=100)
    assert stored["total"] == result["finding_count"]
    assert all(item["review_state"] == "candidate" for item in stored["items"])


def test_review_is_explicit_and_preserves_history(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    result = run_quality_scan(db)
    finding = list_findings(db, limit=1)["items"][0]

    reviewed = review_finding(
        db, finding["id"], "accepted_correction", "Reviewed against source evidence"
    )
    assert reviewed["review_state"] == "accepted_correction"
    assert reviewed["resolution_note"] == "Reviewed against source evidence"
    assert quality_report(db)["review_states"] == {
        "accepted_correction": 1,
        "candidate": result["finding_count"] - 1,
    }
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_reviews").fetchone()[0] == 1


def test_v13_database_migrates_to_quality_tables_without_erasing_observations(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE data_quality_reviews")
        conn.execute("DROP TABLE data_quality_findings")
        conn.execute("PRAGMA user_version = 13")
        before = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    conn = _write_connection(db)
    conn.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == before
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_quality_findings'"
        ).fetchone()


def test_failed_quality_migration_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "legacy.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE data_quality_reviews")
        conn.execute("DROP TABLE data_quality_findings")
        conn.execute("PRAGMA user_version = 13")
        before = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]

    original_schema = db_module.SCHEMA_V14_QUALITY_INDEX
    monkeypatch.setattr(
        db_module,
        "SCHEMA_V14_QUALITY_INDEX",
        "CREATE INDEX partial_quality ON data_quality_findings(code); "
        "PRAGMA user_version = 14; CREATE TABLE invalid_quality(",
    )
    with pytest.raises(db_module.StorageError, match="data-quality"):
        _write_connection(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == before
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='partial_quality'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(db_module, "SCHEMA_V14_QUALITY_INDEX", original_schema)
    conn = _write_connection(db)
    conn.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_quality_api_scans_filters_and_reviews_with_managed_token(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    token = "q" * 32
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    with TestClient(app, headers={"x-finintel-process-token": token}) as client:
        assert client.post("/api/v1/quality/scan", json={"max_findings": 10}).status_code == 200
        response = client.get(
            "/api/v1/quality/findings",
            params={"finding_type": "provider_divergence", "limit": 10},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        finding_id = items[0]["id"]
        reviewed = client.post(
            f"/api/v1/quality/findings/{finding_id}/review",
            json={"state": "rejected", "note": "Expected adjustment mismatch"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["review_state"] == "rejected"
