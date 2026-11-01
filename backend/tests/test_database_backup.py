"""Restore safety: complete backups, merge keys, replacement recovery and bad inputs."""

import sqlite3
from datetime import date

import pytest

from finintel.market_data.quality import (
    _fingerprint,
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.storage.backup import (
    create_backup,
    inspect_backup,
    list_backups,
    restore_backup,
)
from finintel.storage.db import StorageError, _write_connection


def seed(path, symbol, close, retrieved):
    conn = _write_connection(path)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO instruments VALUES (?, ?)",
            (symbol, retrieved),
        )
        conn.execute(
            "INSERT OR IGNORE INTO series(symbol,provider,frequency,adjustment,data_kind) "
            "VALUES (?,'alpha_vantage','1d','raw_unadjusted','historical')",
            (symbol,),
        )
        series = conn.execute("SELECT id FROM series WHERE symbol=?", (symbol,)).fetchone()[0]
        run = conn.execute(
            "INSERT INTO ingestion_runs (series_id,retrieved_at,imported_at,payload_sha256,"
            "source_url,provider_terms_url,provider_last_refreshed,first_session,last_session,"
            "observations,inserted,updated,unchanged,stale) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,1,0,0,0)",
            (
                series,
                retrieved,
                retrieved,
                symbol + retrieved,
                "https://www.alphavantage.co/query",
                "https://www.alphavantage.co",
                None,
                "2026-10-20",
                "2026-10-20",
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?,?)",
            (series, "2026-10-20", close, close, close, close, 100, retrieved, run),
        )
        conn.execute(
            "INSERT OR IGNORE INTO symbol_catalog VALUES (?,?,?,?,?,?)",
            (symbol, symbol, "Stock", "XNYS", "test", retrieved),
        )
        conn.execute(
            "INSERT INTO action_events(occurred_at,action,status,symbol,provider,import_id,"
            "error_code,duration_ms) VALUES (?,'acquisition','success',?,"
            "'alpha_vantage',?,NULL,1)",
            (retrieved, symbol, run),
        )
    conn.close()


def symbols(path):
    with sqlite3.connect(path) as db:
        return {
            row[0]: row[1]
            for row in db.execute(
                "SELECT s.symbol,p.close FROM series s JOIN daily_prices p ON s.id=p.series_id"
            )
        }


def test_manual_backup_only(tmp_path):
    path = tmp_path / "finintel.sqlite3"
    seed(path, "AAPL", "90", "2026-10-20T00:00:00")
    assert list_backups(db=path) == []
    manual = create_backup(db=path)
    assert len(list_backups(db=path)) == 1
    inspected = inspect_backup(manual["id"], db=path)
    assert inspected["counts"]["instruments"] == 1
    assert inspected["schema_version"] == 14
    # The UI preview stays lightweight even for tens of millions of price rows.
    assert "daily_prices" not in inspected["counts"]
    assert manual["reason"] == "manual"
    # Routine database operations do not create scheduled snapshots.
    seed(path, "MSFT", "120", "2026-10-20T00:00:00")
    assert len(list_backups(db=path)) == 1


def test_merge_preserves_newer_prices_and_adds_missing_instruments(tmp_path):
    target = tmp_path / "main.sqlite3"
    source = tmp_path / "source.sqlite3"
    seed(source, "AAPL", "90", "2026-10-20T00:00:00")
    seed(source, "MSFT", "120", "2026-10-20T00:00:00")
    snapshot = create_backup(db=source)
    seed(target, "AAPL", "100", "2026-10-21T00:00:00")
    seed(target, "NVDA", "150", "2026-10-21T00:00:00")

    result = restore_backup(snapshot["id"], "merge", db=target)
    assert result["status"] == "restored"
    assert symbols(target) == {"AAPL": "100", "MSFT": "120", "NVDA": "150"}
    assert result["recovery_backup"]["reason"] == "pre-restore"
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merge_imports_quality_findings_reviews_and_scan_metadata(tmp_path):
    target = tmp_path / "main.sqlite3"
    source = tmp_path / "source.sqlite3"
    seed(source, "AAPL", "90", "2026-10-20T00:00:00")
    with sqlite3.connect(source) as db:
        series_id, ingestion_id = db.execute(
            "SELECT s.id,p.ingestion_id FROM series s "
            "JOIN daily_prices p ON p.series_id=s.id WHERE s.symbol='AAPL'"
        ).fetchone()
        db.execute(
            "INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?,?)",
            (
                series_id,
                "2026-10-21",
                "10",
                "9",
                "8",
                "10",
                100,
                "2026-10-20T00:00:00",
                ingestion_id,
            ),
        )
    scan = run_quality_scan(source)
    finding = list_findings(source, limit=1)["items"][0]
    review_finding(source, finding["id"], "confirmed", "Reviewed in source backup")
    snapshot = create_backup(db=source)

    # Insert another series first so the incoming AAPL series ID must be
    # remapped rather than accidentally reused in the destination.
    seed(target, "NVDA", "150", "2026-10-21T00:00:00")
    seed(target, "AAPL", "100", "2026-10-21T00:00:00")
    restore_backup(snapshot["id"], "merge", db=target)

    with sqlite3.connect(target) as db:
        finding_rows = db.execute(
            "SELECT id,series_id,fingerprint,review_state,resolution_note "
            "FROM data_quality_findings ORDER BY id"
        ).fetchall()
        assert len(finding_rows) == scan["findings_stored"]
        target_aapl_series = db.execute("SELECT id FROM series WHERE symbol='AAPL'").fetchone()[0]
        assert all(row[1] in (None, target_aapl_series) for row in finding_rows)
        confirmed = [row for row in finding_rows if row[3] == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0][4] == "Reviewed in source backup"
        assert db.execute("SELECT COUNT(*) FROM data_quality_reviews").fetchone()[0] == 1
        assert (
            db.execute("SELECT finding_id FROM data_quality_reviews").fetchone()[0]
            == confirmed[0][0]
        )
        assert db.execute("SELECT COUNT(*) FROM data_quality_scan_runs").fetchone()[0] == 1
        progress = db.execute(
            "SELECT status,stale,cursor_phase,cursor_source_key FROM data_quality_scan_progress"
        ).fetchone()
        assert progress == ("stale", 1, "equity", None)

    merged_report = quality_report(target, limit=100)
    assert merged_report["status"] == "stale"
    assert merged_report["complete"] is False
    assert merged_report["scan_completeness"]["stale"] is True

    run_quality_scan(target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == len(
            finding_rows
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM data_quality_findings WHERE review_state='confirmed'"
            ).fetchone()[0]
            == 1
        )

    # Re-merging the same backup is idempotent by fingerprint, review event,
    # and scan-run key.
    restore_backup(snapshot["id"], "merge", db=target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == len(
            finding_rows
        )
        assert db.execute("SELECT COUNT(*) FROM data_quality_reviews").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM data_quality_scan_runs").fetchone()[0] == 2


def test_index_finding_fingerprint_survives_backup_merge_and_rescan(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    seed(source, "AAPL", "90", "2026-10-20T00:00:00")
    with _write_connection(source) as conn:
        conn.executemany(
            "INSERT INTO index_daily_closes"
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES ('SP500','fred',?,?,?,?)",
            [
                (
                    "2026-10-19",
                    "5000",
                    "2026-10-31T00:00:00+00:00",
                    "https://fred.stlouisfed.org/series/SP500",
                ),
                (
                    "2026-10-21",
                    "5010",
                    "2026-10-31T00:00:00+00:00",
                    "https://fred.stlouisfed.org/series/SP500",
                ),
            ],
        )
    conn.close()

    scan = run_quality_scan(source, as_of=date(2026, 10, 21))
    index_findings = list_findings(source, finding_type="missing_session", code="SP500", limit=10)
    assert scan["complete"] is True
    assert index_findings["total"] == 1
    source_finding = index_findings["items"][0]
    review_finding(source, source_finding["id"], "confirmed", "Reviewed index gap")
    snapshot = create_backup(db=source)

    _write_connection(target).close()
    restore_backup(snapshot["id"], "merge", db=target)

    expected_fingerprint = _fingerprint(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        session_date="2026-10-20",
        finding_type="missing_session",
        rule="calendar_missing_session",
        adjustment="price_index",
        frequency="1d",
    )
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT fingerprint,review_state FROM data_quality_findings "
            "WHERE instrument_kind='index' AND code='SP500'"
        ).fetchone()
        assert row == (expected_fingerprint, "confirmed")

    run_quality_scan(target, as_of=date(2026, 10, 21))
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_findings "
                "WHERE instrument_kind='index' AND code='SP500'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT review_state FROM data_quality_findings "
                "WHERE instrument_kind='index' AND code='SP500'"
            ).fetchone()[0]
            == "confirmed"
        )


def test_index_price_anomaly_fingerprint_survives_backup_merge_and_rescan(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    seed(source, "AAPL", "90", "2026-10-20T00:00:00")
    with _write_connection(source) as conn:
        conn.executemany(
            "INSERT INTO index_daily_closes"
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES ('SP500','fred',?,?,?,?)",
            [
                (
                    "2026-10-19",
                    "5000",
                    "2026-10-31T00:00:00+00:00",
                    "https://fred.stlouisfed.org/series/SP500",
                ),
                (
                    "2026-10-20",
                    "0",
                    "2026-10-31T00:00:00+00:00",
                    "https://fred.stlouisfed.org/series/SP500",
                ),
            ],
        )
    conn.close()

    scan = run_quality_scan(source, as_of=date(2026, 10, 20))
    index_findings = list_findings(source, finding_type="price_anomaly", code="SP500", limit=10)
    assert scan["complete"] is True
    assert index_findings["total"] == 1
    source_finding = index_findings["items"][0]
    assert source_finding["rule"] == "positive_finite_index_close"
    review_finding(source, source_finding["id"], "confirmed", "Reviewed index close")
    snapshot = create_backup(db=source)

    _write_connection(target).close()
    restore_backup(snapshot["id"], "merge", db=target)

    expected_fingerprint = _fingerprint(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        session_date="2026-10-20",
        finding_type="price_anomaly",
        rule="positive_finite_index_close",
        adjustment="price_index",
        frequency="1d",
    )
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT fingerprint,review_state FROM data_quality_findings "
            "WHERE instrument_kind='index' AND code='SP500' "
            "AND finding_type='price_anomaly'"
        ).fetchone()
        assert row == (expected_fingerprint, "confirmed")

    run_quality_scan(target, as_of=date(2026, 10, 20))
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_findings "
                "WHERE instrument_kind='index' AND code='SP500' "
                "AND finding_type='price_anomaly'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT review_state FROM data_quality_findings "
                "WHERE instrument_kind='index' AND code='SP500' "
                "AND finding_type='price_anomaly'"
            ).fetchone()[0]
            == "confirmed"
        )


def test_replace_and_undo_using_recovery_backup(tmp_path):
    target = tmp_path / "main.sqlite3"
    source = tmp_path / "source.sqlite3"
    seed(source, "MSFT", "120", "2026-10-20T00:00:00")
    snapshot = create_backup(db=source)
    seed(target, "AAPL", "100", "2026-10-21T00:00:00")

    first = restore_backup(snapshot["id"], "replace", db=target)
    assert symbols(target) == {"MSFT": "120"}
    recovery_id = first["recovery_backup"]["id"]
    restore_backup(recovery_id, "replace", db=target)
    assert symbols(target) == {"AAPL": "100"}


def test_invalid_mode_id_and_corruption_never_change_destination(tmp_path):
    target = tmp_path / "main.sqlite3"
    seed(target, "AAPL", "100", "2026-10-20T00:00:00")
    saved = create_backup(db=target)
    with pytest.raises(ValueError):
        restore_backup(saved["id"], "cancel", db=target)
    with pytest.raises(StorageError):
        restore_backup("../../elsewhere.sqlite3", "replace", db=target)
    (target.parent / "backups" / saved["id"]).write_bytes(b"not sqlite")
    with pytest.raises(StorageError):
        inspect_backup(saved["id"], db=target)
    with pytest.raises(StorageError):
        restore_backup(saved["id"], "replace", db=target)
    assert symbols(target) == {"AAPL": "100"}
