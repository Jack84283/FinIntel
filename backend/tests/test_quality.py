"""Durable data-quality candidate and review behavior."""

import sqlite3
import threading
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data import quality as quality_module
from finintel.market_data.quality import (
    _candidate,
    _persist,
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.storage import db as db_module
from finintel.storage.db import STORAGE_WRITE_LOCK, _write_connection


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


def test_quality_fingerprint_includes_full_series_identity(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
            "VALUES (?, 'AAPL', 'alpaca_sip', ?, ?, 'historical_eod')",
            [(4, "1wk", "raw_unadjusted"), (5, "1d", "adjusted")],
        )

    common = {
        "code": "AAPL",
        "provider": "alpaca_sip",
        "session_date": "2026-10-21",
        "finding_type": "ohlcv_anomaly",
        "severity": "error",
        "confidence": "high",
        "rule": "validated_ohlcv_constraints",
        "evidence": {"invalid_fields": ["ohlc_order"]},
    }
    candidates = [
        _candidate(series_id=1, adjustment="raw_unadjusted", frequency="1d", **common),
        _candidate(series_id=4, adjustment="raw_unadjusted", frequency="1wk", **common),
        _candidate(series_id=5, adjustment="adjusted", frequency="1d", **common),
    ]
    assert len({item["fingerprint"] for item in candidates}) == 3

    _persist(db, candidates)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == 3
    _persist(db, candidates)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == 3


def test_repeated_quality_scans_reuse_finding_rows(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    first = run_quality_scan(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT id, fingerprint FROM data_quality_findings ORDER BY id"
        ).fetchall()

    second = run_quality_scan(db)
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT id, fingerprint FROM data_quality_findings ORDER BY id"
        ).fetchall()

    assert second["complete"] is True
    assert second["truncated"] is False
    assert second["finding_count"] == first["finding_count"]
    assert after == before


def test_quality_scan_reports_truncation_and_report_counts_all_known_findings(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    result = run_quality_scan(db, max_findings=2)
    assert result["status"] == "partial"
    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["findings_examined"] > result["findings_stored"] == 2
    assert result["total_known_findings"] == result["findings_examined"]
    assert result["persisted_findings_total"] == 2

    report = quality_report(db, limit=1)
    assert report["status"] == "partial"
    assert report["truncated"] is True
    assert report["total_known_findings"] == 2
    assert report["findings_stored"] == 2
    assert report["returned_count"] == 1
    assert sum(report["review_states"].values()) == 2


def test_quality_scan_does_not_hold_write_lock_during_read_only_scan(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors = []
    original = quality_module._read_candidates

    def blocked_read(path, max_findings):
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the read phase")
        return original(path, max_findings)

    monkeypatch.setattr(quality_module, "_read_candidates", blocked_read)

    def scan():
        try:
            results.append(run_quality_scan(db))
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            db_module.StorageError,
        ) as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=scan)
    thread.start()
    assert entered.wait(5)
    acquired = STORAGE_WRITE_LOCK.acquire(timeout=1)
    release.set()
    if acquired:
        STORAGE_WRITE_LOCK.release()
    thread.join(10)

    assert acquired is True
    assert not thread.is_alive()
    assert errors == []
    assert results and results[0]["status"] == "completed"


def test_cross_provider_scan_handles_realistic_history_without_materializing_rows(tmp_path):
    db = tmp_path / "performance.sqlite3"
    symbols = [f"T{index:04d}" for index in range(12)]
    sessions = [date(2025, 1, 2) + timedelta(days=index) for index in range(252)]
    conn = _write_connection(db)
    conn.executemany(
        "INSERT INTO instruments(symbol, created_at) VALUES (?, '2026-10-31')",
        [(symbol,) for symbol in symbols],
    )
    series_rows = []
    ingestion_rows = []
    price_rows = []
    next_series = 100
    for symbol in symbols:
        for provider, offset in (("alpaca_sip", "0"), ("tiingo", "2")):
            series_id = next_series
            next_series += 1
            series_rows.append(
                (series_id, symbol, provider, "1d", "raw_unadjusted", "historical_eod")
            )
            ingestion_rows.append(
                (
                    series_id,
                    series_id,
                    f"performance-{series_id}",
                    f"https://example.test/{provider}",
                )
            )
            price_rows.extend(
                (
                    series_id,
                    session.isoformat(),
                    str(98 + index),
                    str(103 + index),
                    str(97 + index),
                    str(100 + index + int(offset)),
                    1000,
                    series_id,
                )
                for index, session in enumerate(sessions)
            )
    conn.executemany(
        "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        series_rows,
    )
    conn.executemany(
        "INSERT INTO ingestion_runs "
        "(id, series_id, retrieved_at, imported_at, payload_sha256, source_url, "
        "provider_terms_url, first_session, last_session, observations, inserted, updated, unchanged, stale) "
        "VALUES (?, ?, '2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00', ?, ?, "
        "'https://example.test/terms', '2025-01-02', '2025-09-10', 252, 252, 0, 0, 0)",
        ingestion_rows,
    )
    conn.executemany(
        "INSERT INTO daily_prices "
        "(series_id, session_date, open, high, low, close, volume, retrieved_at, ingestion_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-31T00:00:00+00:00', ?)",
        price_rows,
    )
    conn.commit()
    conn.close()

    started = time.monotonic()
    result = run_quality_scan(db, max_findings=20)
    elapsed = time.monotonic() - started
    assert result["status"] == "partial"
    assert result["findings_examined"] > result["findings_stored"] == 20
    assert elapsed < 10


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
        scan = client.post("/api/v1/quality/scan", json={"max_findings": 10})
        assert scan.status_code == 200
        scan_payload = scan.json()
        assert scan_payload["findings_examined"] == scan_payload["total_known_findings"]
        assert scan_payload["findings_stored"] == scan_payload["finding_count"]
        assert scan_payload["truncated"] is False
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
        report = client.get("/api/v1/quality/report", params={"limit": 1})
        assert report.status_code == 200
        assert report.json()["truncated"] is True
        assert report.json()["total_known_findings"] == scan_payload["findings_stored"]
