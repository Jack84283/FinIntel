"""Local-only Issue #29 evidence from a detached SQLite backup copy.

This does not run a quality scan, migrate, restore, call providers, or imply
production acceptance. No raw market observations are exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

EXPECTED_SCHEMA = 15
MAX_REPEAT = 10
MAX_SAMPLE = 500
COUNT_TABLES = (
    "daily_prices",
    "index_daily_closes",
    "series",
    "ingestion_runs",
    "data_quality_findings",
    "data_quality_reviews",
    "data_quality_scan_runs",
    "data_quality_scan_progress",
    "quality_corrections",
    "quality_correction_events",
    "corporate_action_evidence",
)


def _detached_copy(path: Path) -> Path:
    """Reject obvious active databases, symlinks and uncheckpointed sidecars."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Use an existing regular SQLite backup copy, not a symlink")
    actual = path.resolve()
    active = Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3"))
    if actual in {active.expanduser().resolve(), Path("data/finintel.sqlite3").resolve()}:
        raise ValueError("Refusing to inspect a configured or default active database")
    if any(
        Path(str(actual) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("Backup has SQLite sidecars; first make a detached checkpointed backup")
    return actual


def _read_db(path: Path) -> sqlite3.Connection:
    # immutable=1 prevents journal creation only for detached snapshots.
    # An active WAL database must not be passed because immutable ignores WAL writes.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Caller table names are fixed literals, never read from the database.
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _measure(
    conn: sqlite3.Connection,
    label: str,
    sql: str,
    params: tuple,
    *,
    repeats: int,
    expected_columns: dict[str, set[str]],
    table_columns: dict[str, set[str]],
) -> dict:
    missing = {
        table: sorted(columns - table_columns.get(table, set()))
        for table, columns in expected_columns.items()
        if columns - table_columns.get(table, set())
    }
    if missing:
        return {
            "id": label,
            "status": "not_run",
            "reason": "schema_columns_missing",
            "missing_columns": missing,
        }
    elapsed_ms = []
    returned = 0
    for _ in range(repeats):
        started = time.perf_counter()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError:
            return {"id": label, "status": "failed", "reason": "sqlite_query_error"}
        elapsed_ms.append(round((time.perf_counter() - started) * 1000, 3))
        returned = len(rows)
        # Neither serialize nor log the actual query rows.
    return {
        "id": label,
        "status": "measured",
        "repeats": repeats,
        "median_ms": round(statistics.median(elapsed_ms), 3),
        "samples_ms": elapsed_ms,
        "rows_seen_in_memory": returned,
    }


def _resource_snapshot() -> dict:
    """Process-only metrics; not a machine-wide peak or full-scan benchmark."""
    result = {"cpu_seconds": round(time.process_time(), 3)}
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        factor = 1 if sys.platform == "darwin" else 1024
        result["process_peak_rss_bytes"] = int(maxrss * factor)
    except (ImportError, OSError, AttributeError):
        result["process_peak_rss_bytes"] = None
    return result


def inspect(
    copy: Path,
    *,
    repeats: int,
    sample: int,
    quick_check: bool,
    foreign_keys: bool,
    source_digests: bool = False,
) -> dict:
    """Collect numeric-only evidence without writing to the inspected copy."""
    source = _detached_copy(copy)
    initial = source.stat()
    starting = time.perf_counter()
    cpu_before = time.process_time()
    with _read_db(source) as conn:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"daily_prices", "index_daily_closes", "data_quality_findings"}
        table_columns = {
            table: _columns(conn, table) for table in COUNT_TABLES if table in tables
        }
        counts = {}
        for table in COUNT_TABLES:
            if table in tables:
                counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
        provenance = {}
        if {"provider"}.issubset(table_columns.get("series", set())):
            provenance["equity_provider_count"] = conn.execute(
                "SELECT COUNT(DISTINCT provider) FROM series"
            ).fetchone()[0]
        if {"provider"}.issubset(table_columns.get("index_daily_closes", set())):
            provenance["index_provider_count"] = conn.execute(
                "SELECT COUNT(DISTINCT provider) FROM index_daily_closes"
            ).fetchone()[0]
        checks = {}
        if quick_check:
            try:
                checks["sqlite_quick_check"] = (
                    "pass"
                    if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    else "fail"
                )
            except sqlite3.DatabaseError:
                checks["sqlite_quick_check"] = "fail"
        else:
            checks["sqlite_quick_check"] = "not_run"
        if foreign_keys:
            try:
                checks["foreign_key_check"] = (
                    "fail"
                    if conn.execute("PRAGMA foreign_key_check").fetchone()
                    else "pass"
                )
            except sqlite3.DatabaseError:
                checks["foreign_key_check"] = "fail"
        else:
            checks["foreign_key_check"] = "not_run"
        digests = {"status": "not_run"}
        if source_digests:
            digests = {}
            # Stream rows in stable primary-key order; report only SHA-256 digests.
            digest_queries = {
                "daily_prices": (
                    (
                        "series_id",
                        "session_date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "retrieved_at",
                        "ingestion_id",
                    ),
                    "series_id,session_date",
                ),
                "index_daily_closes": (
                    (
                        "index_code",
                        "provider",
                        "session_date",
                        "close",
                        "retrieved_at",
                        "source_url",
                    ),
                    "index_code,provider,session_date",
                ),
            }
            for table, (columns, order) in digest_queries.items():
                if not set(columns).issubset(table_columns.get(table, set())):
                    digests[table] = {
                        "status": "not_run",
                        "reason": "schema_columns_missing",
                    }
                    continue
                digest = hashlib.sha256()
                count = 0
                # SQL identifiers are fixed literals in digest_queries.
                sql = f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"
                for row in conn.execute(sql):
                    digest.update(
                        json.dumps(
                            tuple(row), separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                    )
                    digest.update(b"\n")
                    count += 1
                digests[table] = {
                    "status": "computed",
                    "sha256": digest.hexdigest(),
                    "rows_hashed": count,
                }
        queries = [
            _measure(
                conn,
                "sp500_index_recent",
                "SELECT session_date FROM index_daily_closes "
                "WHERE index_code=? ORDER BY session_date DESC LIMIT ?",
                ("SP500", sample),
                repeats=repeats,
                expected_columns={"index_daily_closes": {"index_code", "session_date"}},
                table_columns=table_columns,
            ),
            _measure(
                conn,
                "sp500_active_findings_recent",
                "SELECT id FROM data_quality_findings "
                "WHERE code=? AND superseded_at IS NULL ORDER BY id DESC LIMIT ?",
                ("SP500", sample),
                repeats=repeats,
                expected_columns={
                    "data_quality_findings": {"id", "code", "superseded_at"}
                },
                table_columns=table_columns,
            ),
            _measure(
                conn,
                "single_equity_recent",
                "SELECT session_date FROM daily_prices "
                "WHERE series_id=(SELECT MIN(id) FROM series) "
                "ORDER BY session_date DESC LIMIT ?",
                (sample,),
                repeats=repeats,
                expected_columns={
                    "daily_prices": {"series_id", "session_date"},
                    "series": {"id"},
                },
                table_columns=table_columns,
            ),
        ]
    final = source.stat()
    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise RuntimeError("Detached database changed during inspection; discard evidence")
    observed = _resource_snapshot()
    observed["inspection_wall_seconds"] = round(time.perf_counter() - starting, 3)
    observed["inspection_cpu_seconds"] = round(time.process_time() - cpu_before, 3)
    return {
        "format": "finintel-issue29-private-evidence-v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "scope": "detached_backup_read_only",
        "production_acceptance": "unverified",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logical_cpu_count": os.cpu_count(),
        },
        "database": {
            "schema_version": schema_version,
            "expected_schema_version": EXPECTED_SCHEMA,
            "schema_version_matches": schema_version == EXPECTED_SCHEMA,
            "file_size_bytes": initial.st_size,
            "missing_core_tables": sorted(required - tables),
            "counts": counts,
            "provider_family_counts": provenance,
        },
        "checks": checks,
        "bounded_queries": queries,
        "source_digests": digests,
        "process_resources": observed,
        "unverified": [
            "full_resumable_quality_scan",
            "backup_restore_merge_replace",
            "native_desktop_manual_qa",
            "live_provider_semantics",
            "signoff",
        ],
        "disclosure": (
            "Numbers and timings only; no symbols, file paths, source URLs, "
            "credentials or price samples. Keep this file private until reviewed."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect-db",
        type=Path,
        required=True,
        help="Existing detached SQLite backup copy; never the active database",
    )
    parser.add_argument("--output", type=Path, required=True, help="New private JSON filename")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--sample", type=int, default=100)
    parser.add_argument(
        "--source-digests",
        action="store_true",
        help="Stream all OHLCV/index rows into private SHA-256 digests; may take minutes",
    )
    parser.add_argument(
        "--quick-check",
        action="store_true",
        help="Explicit SQLite quick_check; may be slow on large files",
    )
    parser.add_argument(
        "--foreign-key-check",
        action="store_true",
        help="Explicit FK check; may be slow on large files",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= MAX_REPEAT:
        parser.error(f"--repeat must be between 1 and {MAX_REPEAT}")
    if not 1 <= args.sample <= MAX_SAMPLE:
        parser.error(f"--sample must be between 1 and {MAX_SAMPLE}")
    output = args.output.expanduser()
    source = args.inspect_db.expanduser()
    forbidden_outputs = {
        source.resolve(),
        *(
            Path(str(source.resolve()) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        ),
    }
    if output.resolve() in forbidden_outputs:
        parser.error("Output would collide with a database or its SQLite sidecars")
    if output.exists() or output.is_symlink():
        parser.error("Output already exists; choose a new file name")
    report = inspect(
        source,
        repeats=args.repeat,
        sample=args.sample,
        quick_check=args.quick_check,
        foreign_keys=args.foreign_key_check,
        source_digests=args.source_digests,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("Issue #29 private inspection written. Production acceptance remains unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
