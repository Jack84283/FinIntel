"""Restore safety: complete backups, merge keys, replacement recovery and bad inputs."""

import sqlite3

import pytest

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
    assert inspected["schema_version"] == 7
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
