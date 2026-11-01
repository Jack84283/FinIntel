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

from finintel.storage.db import (
    SCHEMA_VERSION,
    StorageError,
    _write_connection,
    database_path,
    serialized_storage_write,
)

_LOCK = threading.Lock()
_BACKUP_ID = re.compile(
    r"^finintel-(?:auto|manual|pre-restore)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}[.]sqlite3$"
)


def _directory(path: Path) -> Path:
    return path.parent / "backups"


def _validated(path: Path, *, version: bool = True) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise StorageError("SQLite backup is missing or unsafe")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
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
                    "instruments",
                    "series",
                    "daily_prices",
                    "ingestion_runs",
                    "symbol_catalog",
                    "action_events",
                    "index_memberships",
                    "index_daily_closes",
                    "index_series_catalog",
                    "index_ingestion_runs",
                    "index_membership_snapshots",
                    "data_quality_findings",
                    "data_quality_reviews",
                    "data_quality_scan_runs",
                )
                if table in tables
            }
            return {"schema_version": actual, "counts": counts}
    except sqlite3.Error as exc:
        raise StorageError("SQLite backup could not be validated") from exc


def _copy_sqlite(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) as incoming,
        sqlite3.connect(destination, timeout=30) as output,
    ):
        incoming.backup(output, pages=1000, sleep=0.05)


def _snapshot(path: Path, reason: str) -> dict[str, object]:
    if reason not in ("manual", "pre-restore"):
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
        "reason": "pre-restore"
        if path.name.startswith("finintel-pre-restore-")
        else path.name.split("-")[1],
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
        [
            _entry(p)
            for p in folder.iterdir()
            if _BACKUP_ID.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        ],
        key=lambda row: str(row["created_at"]),
        reverse=True,
    )


@serialized_storage_write
def create_backup(*, db: Path | None = None, reason: str = "manual") -> dict[str, object]:
    path = db if db is not None else database_path()
    if not _LOCK.acquire(blocking=False):
        raise StorageError("A database backup or restore is already running")
    try:
        return _snapshot(path, reason)
    finally:
        _LOCK.release()


def inspect_backup(identifier: str, *, db: Path | None = None) -> dict[str, object]:
    """Fast, read-only preview. Full integrity checks occur before any restore write.

    Do not run quick_check, foreign_key_check, or COUNT(*) over daily_prices here:
    those scan tens of millions of historical bars and would block the UI.
    """
    path = db if db is not None else database_path()
    backup = _resolve(path, identifier)
    try:
        with sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"instruments", "series", "ingestion_runs", "daily_prices"}.issubset(tables):
                raise StorageError("Backup lacks required market data tables")
            if not 1 <= version <= SCHEMA_VERSION:
                raise StorageError("Backup schema is not supported by this FinIntel version")
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "instruments",
                    "series",
                    "ingestion_runs",
                    "symbol_catalog",
                    "action_events",
                    "index_memberships",
                    "index_daily_closes",
                    "index_series_catalog",
                    "index_ingestion_runs",
                    "index_membership_snapshots",
                    "data_quality_findings",
                    "data_quality_reviews",
                    "data_quality_scan_runs",
                )
                if table in tables
            }
            return {**_entry(backup), "schema_version": version, "counts": counts}
    except sqlite3.Error as exc:
        raise StorageError("Could not read SQLite backup metadata") from exc


def _merge(staging: Path, source: Path) -> None:
    """Merge supported data; use source IDs only through mapping tables."""
    with sqlite3.connect(staging, timeout=30) as dest:
        dest.execute("PRAGMA foreign_keys = ON")
        dest.execute("ATTACH DATABASE ? AS incoming", (str(source.resolve()),))
        try:
            incoming_tables = {
                row[0]
                for row in dest.execute(
                    "SELECT name FROM incoming.sqlite_master WHERE type='table'"
                )
            }
            with dest:
                dest.execute("INSERT OR IGNORE INTO instruments SELECT * FROM incoming.instruments")
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
                if "data_quality_findings" in incoming_tables:
                    # Findings refer to source-database series IDs. Insert by
                    # fingerprint and then map every incoming finding ID to the
                    # destination row so review history remains attached.
                    dest.execute(
                        "INSERT INTO data_quality_findings("
                        "instrument_kind,code,series_id,provider,session_date,finding_type,"
                        "severity,confidence,rule,evidence_json,fingerprint,review_state,"
                        "resolution_note,created_at,updated_at) "
                        "SELECT f.instrument_kind,f.code,map.new_id,f.provider,f.session_date,"
                        "f.finding_type,f.severity,f.confidence,f.rule,f.evidence_json,"
                        "f.fingerprint,f.review_state,f.resolution_note,f.created_at,f.updated_at "
                        "FROM incoming.data_quality_findings f "
                        "LEFT JOIN _series_map map ON map.old_id=f.series_id "
                        "WHERE f.series_id IS NULL OR map.old_id IS NOT NULL "
                        "ON CONFLICT(fingerprint) DO UPDATE SET "
                        "instrument_kind=excluded.instrument_kind,code=excluded.code,"
                        "series_id=excluded.series_id,provider=excluded.provider,"
                        "session_date=excluded.session_date,finding_type=excluded.finding_type,"
                        "severity=excluded.severity,confidence=excluded.confidence,"
                        "rule=excluded.rule,evidence_json=excluded.evidence_json,"
                        "review_state=excluded.review_state,"
                        "resolution_note=excluded.resolution_note,updated_at=excluded.updated_at "
                        "WHERE excluded.updated_at > data_quality_findings.updated_at"
                    )
                    dest.execute(
                        "CREATE TEMP TABLE _quality_finding_map AS "
                        "SELECT src.id AS old_id,dst.id AS new_id "
                        "FROM incoming.data_quality_findings src "
                        "JOIN data_quality_findings dst ON dst.fingerprint=src.fingerprint"
                    )
                    if "data_quality_reviews" in incoming_tables:
                        dest.execute(
                            "INSERT INTO data_quality_reviews("
                            "finding_id,from_state,to_state,note,reviewed_at) "
                            "SELECT map.new_id,r.from_state,r.to_state,r.note,r.reviewed_at "
                            "FROM incoming.data_quality_reviews r "
                            "JOIN _quality_finding_map map ON map.old_id=r.finding_id "
                            "WHERE NOT EXISTS (SELECT 1 FROM data_quality_reviews current "
                            "WHERE current.finding_id=map.new_id "
                            "AND current.from_state=r.from_state "
                            "AND current.to_state=r.to_state "
                            "AND current.note IS r.note "
                            "AND current.reviewed_at=r.reviewed_at)"
                        )
                    if "data_quality_scan_runs" in incoming_tables:
                        dest.execute(
                            "INSERT OR IGNORE INTO data_quality_scan_runs("
                            "started_at,completed_at,max_findings,findings_examined,"
                            "findings_stored,total_known_findings,persisted_findings_total,"
                            "status,truncated,run_key) "
                            "SELECT started_at,completed_at,max_findings,findings_examined,"
                            "findings_stored,total_known_findings,persisted_findings_total,"
                            "status,truncated,run_key FROM incoming.data_quality_scan_runs"
                        )
                dest.execute(
                    "INSERT OR IGNORE INTO symbol_catalog SELECT * FROM incoming.symbol_catalog"
                )
                # Membership snapshots are indivisible source claims. If the
                # destination already has any rows for an index/effective date,
                # keep that destination snapshot intact rather than combining
                # it with a potentially incompatible source or proxy list.
                dest.execute(
                    "CREATE TEMP TABLE _membership_merge_new AS "
                    "SELECT incoming.index_code,incoming.as_of "
                    "FROM incoming.index_memberships incoming "
                    "GROUP BY incoming.index_code,incoming.as_of "
                    "HAVING NOT EXISTS (SELECT 1 FROM index_memberships current "
                    "WHERE current.index_code=incoming.index_code "
                    "AND current.as_of=incoming.as_of)"
                )
                dest.execute(
                    "INSERT OR IGNORE INTO index_memberships "
                    "SELECT incoming.* FROM incoming.index_memberships incoming "
                    "JOIN _membership_merge_new allowed ON allowed.index_code=incoming.index_code "
                    "AND allowed.as_of=incoming.as_of"
                )
                if "index_membership_snapshots" in incoming_tables:
                    dest.execute(
                        "INSERT OR IGNORE INTO index_membership_snapshots("
                        "index_code,as_of,source,source_kind,source_as_of,expected_count,"
                        "member_count,validation_status,availability_status,confidence,imported_at) "
                        "SELECT incoming.index_code,incoming.as_of,incoming.source,"
                        "incoming.source_kind,incoming.source_as_of,incoming.expected_count,"
                        "incoming.member_count,incoming.validation_status,"
                        "incoming.availability_status,incoming.confidence,incoming.imported_at "
                        "FROM incoming.index_membership_snapshots incoming "
                        "JOIN _membership_merge_new allowed ON allowed.index_code=incoming.index_code "
                        "AND allowed.as_of=incoming.as_of"
                    )
                else:
                    dest.execute(
                        "INSERT OR IGNORE INTO index_membership_snapshots("
                        "index_code,as_of,source,source_kind,source_as_of,expected_count,"
                        "member_count,validation_status,availability_status,confidence,imported_at) "
                        "SELECT incoming.index_code,incoming.as_of,MAX(incoming.source),'unknown',"
                        "incoming.as_of,COUNT(*),COUNT(*),"
                        "'unknown','unverified','unknown',datetime('now') "
                        "FROM incoming.index_memberships incoming "
                        "JOIN _membership_merge_new allowed ON allowed.index_code=incoming.index_code "
                        "AND allowed.as_of=incoming.as_of "
                        "GROUP BY incoming.index_code,incoming.as_of"
                    )
                # A merge may add rows to an already-known snapshot. Reconcile
                # the persisted counts/statuses with the rows that actually
                # survived the conflict rules instead of leaving stale metadata.
                dest.execute(
                    "UPDATE index_membership_snapshots SET member_count=("
                    "SELECT COUNT(*) FROM index_memberships m WHERE "
                    "m.index_code=index_membership_snapshots.index_code AND "
                    "m.as_of=index_membership_snapshots.as_of), "
                    "validation_status=CASE "
                    "WHEN source_kind='unknown' OR expected_count IS NULL THEN 'unknown' "
                    "WHEN expected_count=(SELECT COUNT(*) FROM index_memberships m WHERE "
                    "m.index_code=index_membership_snapshots.index_code AND "
                    "m.as_of=index_membership_snapshots.as_of) THEN 'complete' "
                    "ELSE 'incomplete' END, "
                    "availability_status=CASE "
                    "WHEN source_kind='fund_derived_proxy' THEN 'proxy_only' "
                    "WHEN source_kind='unknown' THEN 'unverified' "
                    "WHEN expected_count IS NULL OR expected_count<>(SELECT COUNT(*) "
                    "FROM index_memberships m WHERE "
                    "m.index_code=index_membership_snapshots.index_code AND "
                    "m.as_of=index_membership_snapshots.as_of) THEN 'incomplete' "
                    "ELSE 'available' END "
                    "WHERE EXISTS (SELECT 1 FROM index_memberships m WHERE "
                    "m.index_code=index_membership_snapshots.index_code AND "
                    "m.as_of=index_membership_snapshots.as_of)"
                )
                dest.execute(
                    "INSERT INTO index_daily_closes("
                    "index_code,provider,session_date,close,retrieved_at,source_url) "
                    "SELECT index_code,provider,session_date,close,retrieved_at,source_url "
                    "FROM incoming.index_daily_closes WHERE 1 "
                    "ON CONFLICT(index_code,provider,session_date) DO UPDATE SET "
                    "close=excluded.close,retrieved_at=excluded.retrieved_at,"
                    "source_url=excluded.source_url "
                    "WHERE excluded.retrieved_at > index_daily_closes.retrieved_at"
                )
                dest.execute(
                    "INSERT INTO index_series_catalog("
                    "index_code,provider,official_name,series_type,frequency,currency,units,"
                    "source_url,local_first_session,local_last_session,coverage_updated_at,"
                    "provider_earliest_available,provider_latest_available,licensing_note,"
                    "known_history_limit) "
                    "SELECT index_code,provider,official_name,series_type,frequency,currency,units,"
                    "source_url,local_first_session,local_last_session,coverage_updated_at,"
                    "provider_earliest_available,provider_latest_available,licensing_note,"
                    "known_history_limit FROM incoming.index_series_catalog WHERE 1 "
                    "ON CONFLICT(index_code,provider) DO UPDATE SET "
                    "official_name=excluded.official_name,series_type=excluded.series_type,"
                    "frequency=excluded.frequency,currency=excluded.currency,units=excluded.units,"
                    "source_url=excluded.source_url,licensing_note=excluded.licensing_note,"
                    "known_history_limit=excluded.known_history_limit,"
                    "provider_earliest_available=COALESCE(excluded.provider_earliest_available,"
                    "index_series_catalog.provider_earliest_available),"
                    "provider_latest_available=COALESCE(excluded.provider_latest_available,"
                    "index_series_catalog.provider_latest_available)"
                )
                dest.execute(
                    "INSERT INTO index_ingestion_runs("
                    "index_code,provider,requested_start,requested_end,retrieved_at,source_url,"
                    "observation_sha256,payload_sha256,first_session,last_session,observations,"
                    "inserted,updated,unchanged) "
                    "SELECT r.index_code,r.provider,r.requested_start,r.requested_end,r.retrieved_at,"
                    "r.source_url,r.observation_sha256,r.payload_sha256,r.first_session,r.last_session,"
                    "r.observations,"
                    "r.inserted,r.updated,r.unchanged FROM incoming.index_ingestion_runs r "
                    "WHERE NOT EXISTS (SELECT 1 FROM index_ingestion_runs current "
                    "WHERE current.index_code=r.index_code AND current.provider=r.provider "
                    "AND current.retrieved_at=r.retrieved_at "
                    "AND current.observation_sha256=r.observation_sha256 "
                    "AND current.payload_sha256 IS r.payload_sha256)"
                )
                dest.execute(
                    "UPDATE index_series_catalog SET "
                    "local_first_session=(SELECT MIN(d.session_date) FROM index_daily_closes d "
                    "WHERE d.index_code=index_series_catalog.index_code "
                    "AND d.provider=index_series_catalog.provider), "
                    "local_last_session=(SELECT MAX(d.session_date) FROM index_daily_closes d "
                    "WHERE d.index_code=index_series_catalog.index_code "
                    "AND d.provider=index_series_catalog.provider), "
                    "coverage_updated_at=COALESCE((SELECT MAX(d.retrieved_at) "
                    "FROM index_daily_closes d WHERE d.index_code=index_series_catalog.index_code "
                    "AND d.provider=index_series_catalog.provider),coverage_updated_at) "
                    "WHERE EXISTS (SELECT 1 FROM index_daily_closes d "
                    "WHERE d.index_code=index_series_catalog.index_code "
                    "AND d.provider=index_series_catalog.provider)"
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
                if "data_quality_findings" in incoming_tables:
                    dest.execute("DROP TABLE _quality_finding_map")
                dest.execute("DROP TABLE _ingestion_map")
                dest.execute("DROP TABLE _series_map")
        finally:
            dest.execute("DETACH DATABASE incoming")


@serialized_storage_write
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
        path.parent.mkdir(parents=True, exist_ok=True)
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
        return {
            "mode": mode,
            "restored": identifier,
            "recovery_backup": recovery,
            "status": "restored",
        }
    except sqlite3.Error as exc:
        raise StorageError("Unable to restore the selected SQLite backup") from exc
    finally:
        _LOCK.release()
