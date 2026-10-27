"""Private SQLite snapshots and explicit, recoverable database restoration.

Backups use sqlite3.Connection.backup to include committed WAL data. A restore
always makes a fresh recovery snapshot, prepares a validated staging database,
and copies it into the destination using SQLite's backup API.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from finintel.storage.db import SCHEMA_VERSION, StorageError, _write_connection, database_path

_LOCK = threading.Lock()
_BACKUP_ID = re.compile(r"^finintel-(?:auto|manual|pre-restore)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\\.sqlite3$")
_RETENTION_DAYS = 7


def _directory(path: Path) -> Path:
    return path.parent / "backups"


def _validated(path: Path, *, version: bool = True) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise StorageError("SQLite backup is missing or unsafe")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("SQLite backup failed integrity verification")
            actual = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version and actual != SCHEMA_VERSION:
                raise StorageError("Backup schema is incompatible with this FinIntel version")
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {"instruments", "series", "ingestion_runs", "daily_prices"}
            if not required.issubset(tables):
                raise StorageError("Backup lacks required market data tables")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageError("Backup has invalid record references")
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "instruments", "series", "daily_prices", "ingestion_runs",
                    "symbol_catalog", "action_events", "index_memberships",
                )
                if table in tables
            }
            return {"schema_version": actual, "counts": counts}
    except sqlite3.Error as exc:
        raise StorageError("SQLite backup could not be validated") from exc


def _copy_sqlite(source: Path, destination: Path) -> None:
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30) as incoming:
        with sqlite3.connect(destination, timeout=30) as output:
            incoming.backup(output, pages=1000, sleep=0.05)


def _snapshot(path: Path, reason: str) -> dict[str, object]:
    if reason not in ("auto", "manual", "pre-restore"):
        raise ValueError("Invalid database backup reason")
    if not path.is_file() or path.is_symlink():
        raise StorageError("No existing FinIntel database to back up")
    folder = _directory(path)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"finintel-{reason}-{stamp}-{uuid4().hex[:12]}.sqlite3"
    final = folder / name
    temporary = folder / ("." + name + ".partial")
    try:
        _copy_sqlite(path, temporary)
        _validated(temporary, version=False)
        temporary.chmod(0o600)
        temporary.replace(final)
        return _entry(final)
    except (sqlite3.Error, OSError):
        raise StorageError("Could not create a verified SQLite backup") from None
    finally:
        temporary.unlink(missing_ok=True)


def _entry(path: Path) -> dict[str, object]:
    return {
        "id": path.name,
        "reason": path.name.split("-")[1],
        "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        "size_bytes": path.stat().st_size,
    }


def _resolve(path: Path, identifier: str) -> Path:
    if not _BACKUP_ID.fullmatch(identifier):
        raise StorageError("Invalid backup identifier")
    backup = _directory(path) / identifier
    if backup.is_symlink() or not backup.is_file():
        raise StorageError("Requested backup does not exist")
    return backup


def list_backups(*, db: Path | None = None) -> list[dict[str, object]]:
    path = db if db is not None else database_path()
    folder = _directory(path)
    if not folder.is_dir():
        return []
    return sorted(
        [_entry(p) for p in folder.iterdir() if _BACKUP_ID.fullmatch(p.name) and p.is_file()
         and not p.is_symlink()],
        key=lambda row: str(row["created_at"]),
        reverse=True,
    )


def create_backup(*, db: Path | None = None, reason: str = "manual") -> dict[str, object]:
    path = db if db is not None else database_path()
    if not _LOCK.acquire(blocking=False):
        raise StorageError("A database backup or restore is already running")
    try:
        return _snapshot(path, reason)
    finally:
        _LOCK.release()


def daily_backup(*, db: Path | None = None) -> dict[str, object] | None:
    """Idempotent daily startup snapshot. Keep the last seven automatic snapshots."""
    path = db if db is not None else database_path()
    if not path.is_file() or not _LOCK.acquire(blocking=False):
        return None
    try:
        today = datetime.now(UTC).strftime("%Y%m%d")
        existing = [row for row in list_backups(db=path)
                    if row["reason"] == "auto" and str(row["id"]).split("-")[2].startswith(today)]
        if existing:
            return None
        result = _snapshot(path, "auto")
        automatic = [row for row in list_backups(db=path) if row["reason"] == "auto"]
        for old in automatic[_RETENTION_DAYS:]:
            _resolve(path, str(old["id"])).unlink()
        return result
    finally:
        _LOCK.release()


def inspect_backup(identifier: str, *, db: Path | None = None) -> dict[str, object]:
    path = db if db is not None else database_path()
    backup = _resolve(path, identifier)
    return {**_entry(backup), **_validated(backup, version=False)}


def _merge(staging: Path, source: Path) -> None:
    """Merge all schema-v6 data; use original source IDs only through mapping tables."""
    with sqlite3.connect(staging, timeout=30) as dest:
        dest.execute("PRAGMA foreign_keys = ON")
        dest.execute("ATTACH DATABASE ? AS incoming", (source.as_uri() + "?mode=ro",))
        try:
            with dest:
                dest.execute(
                    "INSERT OR IGNORE INTO instruments SELECT * FROM incoming.instruments"
                )
                dest.execute(
                    "INSERT OR IGNORE INTO series(symbol,provider,frequency,adjustment,data_kind) "
                    "SELECT symbol,provider,frequency,adjustment,data_kind FROM incoming.series"
                )
                dest.execute(
                    "CREATE TEMP TABLE _series_map AS "
                    "SELECT src.id AS old_id, dst.id AS new_id FROM incoming.series src "
                    "JOIN series dst ON src.symbol=dst.symbol AND src.provider=dst.provider "
                    "AND src.frequency=dst.frequency AND src.adjustment=dst.adjustment"
                )
                dest.execute(
                    "INSERT INTO ingestion_runs("
                    "series_id,retrieved_at,imported_at,payload_sha256,source_url,"
                    "provider_terms_url,provider_last_refreshed,first_session,last_session,"
                    "observations,inserted,updated,unchanged,stale) "
                    "SELECT map.new_id,r.retrieved_at,r.imported_at,r.payload_sha256,"
                    "r.source_url,r.provider_terms_url,r.provider_last_refreshed,"
                    "r.first_session,r.last_session,r.observations,r.inserted,r.updated,"
                    "r.unchanged,r.stale FROM incoming.ingestion_runs r "
                    "JOIN _series_map map ON map.old_id=r.series_id "
                    "WHERE NOT EXISTS (SELECT 1 FROM ingestion_runs current "
                    "WHERE current.series_id=map.new_id "
                    "AND current.imported_at=r.imported_at "
                    "AND current.payload_sha256=r.payload_sha256)"
                )
                dest.execute(
                    "CREATE TEMP TABLE _ingestion_map AS SELECT r.id AS old_id, "
                    "(SELECT MIN(d.id) FROM ingestion_runs d "
                    "WHERE d.series_id=m.new_id AND d.imported_at=r.imported_at "
                    "AND d.payload_sha256=r.payload_sha256) AS new_id "
                    "FROM incoming.ingestion_runs r JOIN _series_map m ON r.series_id=m.old_id"
                )
                dest.execute(
                    "INSERT INTO daily_prices(series_id,session_date,open,high,low,close,"
                    "volume,retrieved_at,ingestion_id) "
                    "SELECT m.new_id,p.session_date,p.open,p.high,p.low,p.close,p.volume,"
                    "p.retrieved_at,i.new_id FROM incoming.daily_prices p "
                    "JOIN _series_map m ON m.old_id=p.series_id "
                    "JOIN _ingestion_map i ON i.old_id=p.ingestion_id WHERE 1 "
                    "ON CONFLICT(series_id,session_date) DO UPDATE SET "
                    "open=excluded.open,high=excluded.high,low=excluded.low,"
                    "close=excluded.close,volume=excluded.volume,"
                    "retrieved_at=excluded.retrieved_at,ingestion_id=excluded.ingestion_id "
                    "WHERE excluded.retrieved_at > daily_prices.retrieved_at"
                )
                dest.execute(
                    "INSERT OR IGNORE INTO symbol_catalog "
                    "SELECT * FROM incoming.symbol_catalog"
                )
                dest.execute(
                    "INSERT OR IGNORE INTO index_memberships "
                    "SELECT * FROM incoming.index_memberships"
                )
                dest.execute(
                    "INSERT INTO action_events("
                    "occurred_at,action,status,symbol,provider,import_id,error_code,duration_ms) "
                    "SELECT e.occurred_at,e.action,e.status,e.symbol,e.provider,"
                    "im.new_id,e.error_code,e.duration_ms FROM incoming.action_events e "
                    "LEFT JOIN _ingestion_map im ON im.old_id=e.import_id "
                    "WHERE NOT EXISTS (SELECT 1 FROM action_events d "
                    "WHERE d.occurred_at=e.occurred_at AND d.action=e.action "
                    "AND d.status=e.status AND d.symbol=e.symbol "
                    "AND d.provider=e.provider AND d.duration_ms=e.duration_ms)"
                )
                dest.execute("DROP TABLE _ingestion_map")
                dest.execute("DROP TABLE _series_map")
        finally:
            dest.execute("DETACH DATABASE incoming")


def restore_backup(identifier: str, mode: str, *, db: Path | None = None) -> dict[str, object]:
    """Cancel is a frontend-only no-op. Merge and replace are explicit writes."""
    if mode not in ("merge", "replace"):
        raise ValueError("Restoration requires merge or replace")
    path = db if db is not None else database_path()
    if not _LOCK.acquire(blocking=False):
        raise StorageError("A database backup or restore is already running")
    try:
        source = _resolve(path, identifier)
        info = _validated(source, version=False)
        if int(info["schema_version"]) > SCHEMA_VERSION or int(info["schema_version"]) == 0:
            raise StorageError("The selected backup has an unsupported schema")
        with tempfile.TemporaryDirectory(prefix="finintel-restore-", dir=path.parent) as work:
            workdir = Path(work)
            incoming = workdir / "incoming.sqlite3"
            _copy_sqlite(source, incoming)
            if int(info["schema_version"]) < SCHEMA_VERSION:
                connection = _write_connection(incoming)
                connection.close()
            _validated(incoming)
            if mode == "merge" and path.is_file():
                staging = workdir / "merged.sqlite3"
                _copy_sqlite(path, staging)
                connection = _write_connection(staging)
                connection.close()
                _merge(staging, incoming)
            else:
                staging = incoming
            _validated(staging)
            # The recovery snapshot is created only after all validation succeeds.
            recovery = _snapshot(path, "pre-restore") if path.is_file() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_sqlite(staging, path)
                _validated(path)
            except (sqlite3.Error, OSError, StorageError) as exc:
                if recovery is not None:
                    _copy_sqlite(_resolve(path, str(recovery["id"])), path)
                raise StorageError("Restore failed; the prior database was recovered") from exc
        return {"mode": mode, "restored": identifier, "recovery_backup": recovery, "status": "restored"}
    except sqlite3.Error as exc:
        raise StorageError("Unable to restore the selected SQLite backup") from exc
    finally:
        _LOCK.release()
