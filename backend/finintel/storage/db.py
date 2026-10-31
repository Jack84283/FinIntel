"""Transactional, versioned SQLite store for historical end-of-day price series.

Only validated HistoricalBatch objects can be imported. API reads never initialize
the database and cannot modify it. PR #3/M1 data persistence (GitHub PR #4).
"""

import hashlib
import os
import sqlite3
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path

from pydantic import ValidationError

from finintel.market_data.models import HistoricalBatch
from finintel.market_data.symbol_catalog import ASSET_TYPES, STARTER_SYMBOLS, synchronized_rows

SCHEMA_VERSION = 10
MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024

# SQLite stores financial price DECIMAL values as TEXT, never REAL, to avoid
# round-off changes. The Pydantic importer enforces OHLC constraints.
SCHEMA_V1 = """
CREATE TABLE instruments (
    symbol TEXT PRIMARY KEY NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE series (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    provider TEXT NOT NULL,
    frequency TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    data_kind TEXT NOT NULL,
    UNIQUE(symbol, provider, frequency, adjustment)
);
CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    retrieved_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    source_url TEXT NOT NULL,
    provider_terms_url TEXT NOT NULL,
    provider_last_refreshed TEXT,
    first_session TEXT NOT NULL,
    last_session TEXT NOT NULL,
    observations INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    stale INTEGER NOT NULL
);
CREATE INDEX ingestion_series_time ON ingestion_runs(series_id, retrieved_at DESC, id DESC);
CREATE TABLE daily_prices (
    series_id INTEGER NOT NULL REFERENCES series(id),
    session_date TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL CHECK(volume >= 0),
    retrieved_at TEXT NOT NULL,
    ingestion_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    PRIMARY KEY(series_id, session_date)
);
CREATE INDEX daily_prices_by_session ON daily_prices(session_date);
"""


# Asset discovery is deliberately separate from observed historical price series:
# a symbol appearing here does not mean prices have been downloaded.
SCHEMA_V2 = """
CREATE TABLE symbol_catalog (
    symbol TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('Stock', 'ETF')),
    exchange TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX symbol_catalog_by_type ON symbol_catalog(asset_type, symbol);
"""


# Version 3 expands the directory taxonomy without modifying price-series tables.
SCHEMA_V3 = """
CREATE TABLE symbol_catalog_v3 (
    symbol TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('Stock', 'ETF', 'Index', 'Mutual Fund')),
    exchange TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO symbol_catalog_v3 SELECT * FROM symbol_catalog;
DROP TABLE symbol_catalog;
ALTER TABLE symbol_catalog_v3 RENAME TO symbol_catalog;
CREATE INDEX symbol_catalog_by_type ON symbol_catalog(asset_type, symbol);
"""


SCHEMA_V4 = """
CREATE TABLE action_events (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('acquisition')),
    status TEXT NOT NULL CHECK(status IN ('success', 'failed')),
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    import_id INTEGER,
    error_code TEXT,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
);
CREATE INDEX action_events_by_time ON action_events(occurred_at DESC, id DESC);
"""

# Membership must be sourced and dated independently of the security asset type.
SCHEMA_V5 = """
CREATE TABLE index_memberships (
    index_code TEXT NOT NULL CHECK(index_code IN ('SP500', 'DJIA')),
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (index_code, symbol, as_of)
);
CREATE INDEX index_membership_symbol ON index_memberships(symbol, index_code);
"""

# Keep every historical SP500/DJIA snapshot while expanding the index universe.
SCHEMA_V6 = """
CREATE TABLE index_memberships_v6 (
    index_code TEXT NOT NULL CHECK(index_code IN ('SP500', 'DJIA', 'NDX', 'RUT')),
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (index_code, symbol, as_of)
);
INSERT INTO index_memberships_v6 SELECT * FROM index_memberships;
DROP TABLE index_memberships;
ALTER TABLE index_memberships_v6 RENAME TO index_memberships;
CREATE INDEX index_membership_symbol ON index_memberships(symbol, index_code);
CREATE INDEX index_membership_snapshot ON index_memberships(index_code, as_of DESC);
"""


# Store index levels separately from equity OHLCV. Index levels have no volume.
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS index_daily_closes (
    index_code TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('fred')),
    session_date TEXT NOT NULL,
    close TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (index_code, provider, session_date)
);
CREATE INDEX IF NOT EXISTS index_daily_closes_by_date
ON index_daily_closes(index_code, provider, session_date DESC);
"""

# Daily acquisition state is durable, resumable, and separate from price rows.
# Settings default to disabled so an existing installation never begins provider
# requests until the user explicitly enables automatic acquisition.
SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS daily_acquisition_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    schedule_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    primary_provider TEXT NOT NULL CHECK(primary_provider IN ('alpaca_sip', 'tiingo', 'alpha_vantage')),
    secondary_provider TEXT CHECK(secondary_provider IS NULL OR secondary_provider IN ('alpaca_sip', 'tiingo', 'alpha_vantage')),
    overlap_days INTEGER NOT NULL CHECK(overlap_days BETWEEN 0 AND 60),
    retry_attempts INTEGER NOT NULL CHECK(retry_attempts BETWEEN 1 AND 5),
    retry_backoff_seconds REAL NOT NULL CHECK(retry_backoff_seconds BETWEEN 0 AND 300),
    catch_up_limit INTEGER NOT NULL DEFAULT 5 CHECK(catch_up_limit BETWEEN 1 AND 30),
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO daily_acquisition_settings
    (id, enabled, schedule_time, timezone, primary_provider, secondary_provider,
     overlap_days, retry_attempts, retry_backoff_seconds, catch_up_limit, updated_at)
VALUES (1, 0, '20:00', 'America/New_York', 'alpaca_sip', 'tiingo', 8, 3, 2, 5, '');

CREATE TABLE IF NOT EXISTS daily_acquisition_jobs (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('automatic', 'manual', 'retry_failed')),
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'running', 'paused', 'completed', 'partial', 'failed', 'cancelled')),
    provider TEXT NOT NULL CHECK(provider IN ('alpaca_sip', 'tiingo', 'alpha_vantage')),
    secondary_provider TEXT CHECK(secondary_provider IS NULL OR secondary_provider IN ('alpaca_sip', 'tiingo', 'alpha_vantage')),
    schedule_date TEXT,
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_heartbeat TEXT,
    current_instrument TEXT,
    total_instruments INTEGER NOT NULL DEFAULT 0 CHECK(total_instruments >= 0),
    completed_instruments INTEGER NOT NULL DEFAULT 0 CHECK(completed_instruments >= 0),
    failed_instruments INTEGER NOT NULL DEFAULT 0 CHECK(failed_instruments >= 0),
    incomplete_instruments INTEGER NOT NULL DEFAULT 0 CHECK(incomplete_instruments >= 0),
    pending_instruments INTEGER NOT NULL DEFAULT 0 CHECK(pending_instruments >= 0),
    successful_imports INTEGER NOT NULL DEFAULT 0 CHECK(successful_imports >= 0),
    inserted INTEGER NOT NULL DEFAULT 0 CHECK(inserted >= 0),
    updated INTEGER NOT NULL DEFAULT 0 CHECK(updated >= 0),
    unchanged INTEGER NOT NULL DEFAULT 0 CHECK(unchanged >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    provider_errors TEXT NOT NULL DEFAULT '[]',
    index_results TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
    pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS daily_acquisition_automatic_date
    ON daily_acquisition_jobs(kind, schedule_date)
    WHERE kind = 'automatic' AND schedule_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS daily_acquisition_jobs_time
    ON daily_acquisition_jobs(scheduled_for DESC, id DESC);

CREATE TABLE IF NOT EXISTS daily_acquisition_items (
    job_id TEXT NOT NULL REFERENCES daily_acquisition_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('equity', 'index')),
    code TEXT NOT NULL,
    provider TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'incomplete', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    started_at TEXT,
    finished_at TEXT,
    last_session TEXT,
    inserted INTEGER NOT NULL DEFAULT 0 CHECK(inserted >= 0),
    updated INTEGER NOT NULL DEFAULT 0 CHECK(updated >= 0),
    unchanged INTEGER NOT NULL DEFAULT 0 CHECK(unchanged >= 0),
    stale INTEGER NOT NULL DEFAULT 0 CHECK(stale >= 0),
    error_type TEXT,
    error_message TEXT,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    missing_sessions INTEGER NOT NULL DEFAULT 0 CHECK(missing_sessions >= 0),
    PRIMARY KEY(job_id, kind, code)
);
CREATE INDEX IF NOT EXISTS daily_acquisition_items_status
    ON daily_acquisition_items(job_id, status, kind, code);

CREATE TABLE IF NOT EXISTS daily_acquisition_lock (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    owner_token TEXT NOT NULL,
    job_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Provider request accounting is metadata only. It never stores credentials or
# response payloads; the scheduler uses it to reserve a request slot before a
# provider HTTP call and to make quota/throttle state visible after restart.
SCHEMA_V10_TABLE = """
CREATE TABLE IF NOT EXISTS daily_acquisition_provider_usage (
    provider TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
    last_request_at TEXT,
    quota_state TEXT NOT NULL DEFAULT 'available',
    quota_message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, usage_date)
);
"""

SCHEMA_V10_INDEX = """
CREATE INDEX IF NOT EXISTS daily_acquisition_provider_usage_date
    ON daily_acquisition_provider_usage(usage_date, provider);
"""

SCHEMA_V10 = SCHEMA_V10_TABLE + SCHEMA_V10_INDEX


class StorageError(Exception):
    """Safe failure message for local CLI or API consumers."""


STORAGE_WRITE_LOCK = threading.RLock()


def serialized_storage_write(function):
    """Serialize writes and database replacement within this backend process."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with STORAGE_WRITE_LOCK:
            return function(*args, **kwargs)

    return wrapped


def database_path() -> Path:
    """Use a single local database path for the CLI and development backend."""
    return Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3")).expanduser()


def _ensure_version(conn: sqlite3.Connection, *, allow_missing: bool) -> bool:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and allow_missing:
        return False
    if version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, SCHEMA_VERSION):
        raise StorageError(
            "SQLite schema version is unsupported; expected version "
            + str(SCHEMA_VERSION)
            + ", found "
            + str(version)
        )
    return True


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise StorageError("Database uses a newer schema; upgrade FinIntel before importing")
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if existing:
            raise StorageError("Refusing to initialize a nonempty unversioned database")
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V1 + "\nPRAGMA user_version = 1;\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to initialize the SQLite schema") from exc
        version = 1
    if version == 1:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V2)
            conn.executemany(
                "INSERT INTO symbol_catalog(symbol, name, asset_type, exchange, source, updated_at) "
                "VALUES (?, ?, ?, ?, 'starter', '')",
                (
                    (item["symbol"], item["name"], item["asset_type"], item["exchange"])
                    for item in STARTER_SYMBOLS
                    if item["asset_type"] in ("Stock", "ETF")
                ),
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate SQLite historical schema to v2") from exc
        version = 2
    if version == 2:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V3)
            conn.executemany(
                "INSERT OR IGNORE INTO symbol_catalog"
                "(symbol, name, asset_type, exchange, source, updated_at) "
                "VALUES (?, ?, ?, ?, 'starter', '')",
                (
                    (item["symbol"], item["name"], item["asset_type"], item["exchange"])
                    for item in STARTER_SYMBOLS
                    if item["asset_type"] in ("Index", "Mutual Fund")
                ),
            )
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
            version = 3
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate SQLite symbol catalog to v3") from exc
    if version == 3:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V4 + "\nPRAGMA user_version = 4;\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate action-event schema to v4") from exc
        version = 4
    if version == 4:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V5 + "\nPRAGMA user_version = 5;\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate index membership schema to v5") from exc
        version = 5
    if version == 5:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V6 + "\nPRAGMA user_version = 6;\nCOMMIT;"
            )
            version = 6
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate index membership schema to v6") from exc
    if version == 6:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V7 + "\nPRAGMA user_version = 7;\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to initialize index-level historical storage v7") from exc
        version = 7
    if version == 7:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V8 + "\nPRAGMA user_version = 8;\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to initialize daily acquisition storage v8") from exc
        version = 8
    if version == 8:
        try:
            conn.execute("BEGIN IMMEDIATE")
            settings_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(daily_acquisition_settings)")
            }
            if "catch_up_limit" not in settings_columns:
                conn.execute(
                    "ALTER TABLE daily_acquisition_settings ADD COLUMN catch_up_limit INTEGER NOT NULL DEFAULT 5"
                )
            job_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(daily_acquisition_jobs)")
            }
            if "incomplete_instruments" not in job_columns:
                conn.execute(
                    "ALTER TABLE daily_acquisition_jobs ADD COLUMN incomplete_instruments INTEGER NOT NULL DEFAULT 0"
                )
            item_schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_acquisition_items'"
            ).fetchone()
            if item_schema and "'incomplete'" not in (item_schema[0] or ""):
                conn.execute(
                    "ALTER TABLE daily_acquisition_items RENAME TO daily_acquisition_items_v8"
                )
                conn.execute("DROP INDEX IF EXISTS daily_acquisition_items_status")
                conn.execute(
                    """CREATE TABLE daily_acquisition_items (
                        job_id TEXT NOT NULL REFERENCES daily_acquisition_jobs(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('equity', 'index')),
                        code TEXT NOT NULL,
                        provider TEXT,
                        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'incomplete', 'failed', 'cancelled')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        started_at TEXT,
                        finished_at TEXT,
                        last_session TEXT,
                        inserted INTEGER NOT NULL DEFAULT 0 CHECK(inserted >= 0),
                        updated INTEGER NOT NULL DEFAULT 0 CHECK(updated >= 0),
                        unchanged INTEGER NOT NULL DEFAULT 0 CHECK(unchanged >= 0),
                        stale INTEGER NOT NULL DEFAULT 0 CHECK(stale >= 0),
                        error_type TEXT,
                        error_message TEXT,
                        validation_status TEXT NOT NULL DEFAULT 'pending',
                        missing_sessions INTEGER NOT NULL DEFAULT 0 CHECK(missing_sessions >= 0),
                        PRIMARY KEY(job_id, kind, code)
                    )"""
                )
                conn.execute(
                    "INSERT INTO daily_acquisition_items SELECT * FROM daily_acquisition_items_v8"
                )
                conn.execute("DROP TABLE daily_acquisition_items_v8")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS daily_acquisition_items_status "
                "ON daily_acquisition_items(job_id, status, kind, code)"
            )
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
            version = 9
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate daily acquisition storage to v9") from exc
    if version == 9:
        try:
            conn.execute("BEGIN IMMEDIATE")
            settings_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(daily_acquisition_settings)")
            }
            if "provider_limits" not in settings_columns:
                conn.execute(
                    "ALTER TABLE daily_acquisition_settings ADD COLUMN provider_limits TEXT "
                    "NOT NULL DEFAULT '{}'"
                )
            # Do not use executescript here: sqlite3 implicitly commits before
            # executing a script, which would make this migration non-atomic.
            conn.execute(SCHEMA_V10_TABLE)
            conn.execute(SCHEMA_V10_INDEX)
            conn.execute("PRAGMA user_version = 10")
            conn.commit()
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to migrate daily acquisition storage to v10") from exc


def _write_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        raise StorageError("Refusing to write to a symlink database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=10)
    except (sqlite3.Error, OSError) as exc:
        raise StorageError("Unable to open the local SQLite database") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if 1 <= current_version < SCHEMA_VERSION:
            # Migrations are destructive for older membership/catalog layouts.
            # Keep the original recoverable before modifying any schema.
            from finintel.storage.backup import _snapshot

            _snapshot(path, "pre-restore")
        _migrate(conn)
        return conn
    except (sqlite3.Error, StorageError):
        conn.close()
        raise


def _read_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        # Read-only URI ensures API calls cannot mutate schema or records.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise StorageError("Could not open the local database read-only") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        _ensure_version(conn, allow_missing=False)
        # Keep all queries made through this connection on one committed snapshot.
        conn.execute("BEGIN")
        return conn
    except (sqlite3.Error, StorageError) as exc:
        conn.close()
        raise StorageError("Local database is unavailable or needs migration") from exc


def _database_version(path: Path) -> int:
    """Read only the schema marker without taking the process write lock."""
    if not path.is_file():
        return 0
    if path.is_symlink():
        raise StorageError("Refusing to read a symlink database path")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise StorageError("Could not inspect the local database schema") from exc


@serialized_storage_write
def _initialize_symbol_catalog(path: Path) -> None:
    """Create or migrate the catalog only when a read cannot use it directly."""
    conn = _write_connection(path)
    conn.close()


def _ensure_symbol_catalog(path: Path) -> None:
    if _database_version(path) < SCHEMA_VERSION:
        _initialize_symbol_catalog(path)


def _safe_source(batch: HistoricalBatch) -> None:
    """Prevent credentials and unwanted remote schemes from entering audit history."""
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(batch.source_url)
    params = parse_qs(url.query)
    if batch.provider == "alpaca_sip":
        if (
            url.scheme != "https"
            or url.hostname != "data.alpaca.markets"
            or url.port is not None
            or url.path != "/v2/stocks/bars"
            or url.username
            or url.password
            or set(params)
            != {"symbols", "timeframe", "start", "end", "feed", "adjustment", "limit"}
            or params.get("symbols") != [batch.symbol]
            or params.get("timeframe") != ["1Day"]
            or params.get("feed") != ["sip"]
            or params.get("adjustment") != ["raw"]
            or params.get("limit") != ["10000"]
        ):
            raise StorageError("Alpaca source metadata failed sanitization")
        from datetime import date as parsed_date

        try:
            first = parsed_date.fromisoformat(params["start"][0])
            last = parsed_date.fromisoformat(params["end"][0])
        except (ValueError, IndexError, KeyError) as exc:
            raise StorageError("Invalid Alpaca date range in source metadata") from exc
        if first > last:
            raise StorageError("Invalid Alpaca source date ordering")
        terms = urlsplit(batch.provider_terms_url)
        if terms.scheme != "https" or terms.hostname != "alpaca.markets":
            raise StorageError("Alpaca provider terms metadata is unapproved")
        return
    if batch.provider == "tiingo":
        if (
            url.scheme != "https"
            or url.hostname != "api.tiingo.com"
            or url.port is not None
            or url.username
            or url.password
            or url.path != "/tiingo/daily/" + batch.symbol + "/prices"
            or set(params) != {"startDate", "endDate"}
            or len(params["startDate"]) != 1
            or len(params["endDate"]) != 1
        ):
            raise StorageError("Tiingo snapshot URL contains invalid metadata")
        try:
            first = date.fromisoformat(params["startDate"][0])
            last = date.fromisoformat(params["endDate"][0])
        except ValueError as exc:
            raise StorageError("Invalid Tiingo date range") from exc
        if first > last or first > batch.first_session or last < batch.last_session:
            raise StorageError("Tiingo source date range conflicts with observations")
        terms = urlsplit(batch.provider_terms_url)
        if (
            terms.scheme != "https"
            or terms.hostname != "www.tiingo.com"
            or terms.path != "/about/terms"
        ):
            raise StorageError("Tiingo license metadata is unapproved")
        return
    if batch.provider != "alpha_vantage":
        raise StorageError("Unknown market-data provider")
    if (
        url.scheme != "https"
        or url.hostname != "www.alphavantage.co"
        or url.port is not None
        or url.path != "/query"
        or url.username
        or url.password
        or not set(params) <= {"function", "symbol", "outputsize", "datatype"}
        or params.get("function") != ["TIME_SERIES_DAILY"]
        or params.get("symbol") != [batch.symbol]
        or ("outputsize" in params and params.get("outputsize") not in (["compact"], ["full"]))
        or ("datatype" in params and params.get("datatype") != ["json"])
    ):
        raise StorageError("Snapshot source metadata is not an approved sanitized provider URL")
    terms = urlsplit(batch.provider_terms_url)
    if (
        terms.scheme != "https"
        or terms.hostname != "www.alphavantage.co"
        or terms.port is not None
        or terms.path not in ("", "/", "/terms_of_service/")
        or terms.query
        or terms.fragment
        or terms.username
        or terms.password
    ):
        raise StorageError("Snapshot license metadata is not from the approved provider")


def load_snapshot(path: Path) -> tuple[HistoricalBatch, str]:
    """Reject invalid local snapshots before the database is opened for writing."""
    try:
        if not path.is_file() or path.is_symlink():
            raise StorageError("Snapshot must be an existing regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_SNAPSHOT_BYTES:
            raise StorageError("Snapshot is empty or exceeds the 25 MiB import limit")
        content = path.read_bytes()
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise StorageError("Snapshot exceeds the 25 MiB import limit")
        batch = HistoricalBatch.model_validate_json(content)
        _safe_source(batch)
        return batch, hashlib.sha256(content).hexdigest()
    except (OSError, ValidationError, ValueError) as exc:
        raise StorageError("Snapshot is unreadable or failed historical-data validation") from exc


@serialized_storage_write
def import_snapshot(snapshot: Path, *, db: Path | None = None) -> dict[str, object]:
    """Upsert OHLCV atomically with an immutable import record for provenance.

    Older provider snapshots may add missing dates but never overwrite a date
    already sourced from a newer retrieval timestamp.
    """
    batch, digest = load_snapshot(snapshot)
    path = db if db is not None else database_path()
    retrieved = batch.retrieved_at.astimezone(UTC).isoformat(timespec="microseconds")
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    conn = _write_connection(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO instruments(symbol, created_at) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO NOTHING",
                (batch.symbol, now),
            )
            conn.execute(
                "INSERT INTO series(symbol, provider, frequency, adjustment, data_kind) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, provider, frequency, adjustment) DO NOTHING",
                (
                    batch.symbol,
                    batch.provider,
                    batch.frequency,
                    batch.price_adjustment,
                    batch.data_kind,
                ),
            )
            series_id = conn.execute(
                "SELECT id FROM series WHERE symbol=? AND provider=? AND frequency=? "
                "AND adjustment=?",
                (batch.symbol, batch.provider, batch.frequency, batch.price_adjustment),
            ).fetchone()["id"]
            cursor = conn.execute(
                "INSERT INTO ingestion_runs("
                "series_id, retrieved_at, imported_at, payload_sha256, "
                "source_url, provider_terms_url, provider_last_refreshed, "
                "first_session, last_session, observations, inserted, updated, unchanged, stale"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)",
                (
                    series_id,
                    retrieved,
                    now,
                    digest,
                    batch.source_url,
                    batch.provider_terms_url,
                    batch.provider_last_refreshed,
                    batch.first_session.isoformat(),
                    batch.last_session.isoformat(),
                    len(batch.observations),
                ),
            )
            run_id = cursor.lastrowid
            inserted = updated = unchanged = stale = 0
            for bar in batch.observations:
                day = bar.session_date.isoformat()
                prices = tuple(str(getattr(bar, key)) for key in ("open", "high", "low", "close"))
                existing = conn.execute(
                    "SELECT open, high, low, close, volume, retrieved_at "
                    "FROM daily_prices WHERE series_id=? AND session_date=?",
                    (series_id, day),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO daily_prices(series_id, session_date, open, high, low, "
                        "close, volume, retrieved_at, ingestion_id) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (series_id, day, *prices, bar.volume, retrieved, run_id),
                    )
                    inserted += 1
                elif existing["retrieved_at"] > retrieved:
                    stale += 1
                elif (
                    all(
                        Decimal(existing[key]) == getattr(bar, key)
                        for key in ("open", "high", "low", "close")
                    )
                    and existing["volume"] == bar.volume
                ):
                    unchanged += 1
                else:
                    conn.execute(
                        "UPDATE daily_prices SET open=?, high=?, low=?, close=?, volume=?, "
                        "retrieved_at=?, ingestion_id=? WHERE series_id=? AND session_date=?",
                        (*prices, bar.volume, retrieved, run_id, series_id, day),
                    )
                    updated += 1
            conn.execute(
                "UPDATE ingestion_runs SET inserted=?, updated=?, unchanged=?, stale=? WHERE id=?",
                (inserted, updated, unchanged, stale, run_id),
            )
        return {
            "import_id": run_id,
            "symbol": batch.symbol,
            "provider": batch.provider,
            "observations": len(batch.observations),
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "stale": stale,
            "database": str(path),
            "source_sha256": digest,
        }
    except sqlite3.Error as exc:
        raise StorageError(
            "SQLite import transaction failed; no partial data was committed"
        ) from exc
    finally:
        conn.close()


def list_instrument_directory(db: Path | None = None) -> list[dict[str, object]]:
    """Fast navigation list: never scan the historical daily_prices table."""
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT symbol, provider, frequency, adjustment, data_kind "
            "FROM series ORDER BY symbol, provider"
        ).fetchall()
        return [
            {**dict(row), "sessions": 0, "first_session": None, "last_session": None}
            for row in rows
        ]
    except sqlite3.Error as exc:
        raise StorageError("Could not read local instrument directory") from exc
    finally:
        conn.close()


def list_instruments(db: Path | None = None) -> list[dict[str, object]]:
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT s.symbol, s.provider, s.frequency, s.adjustment, s.data_kind, "
            "(SELECT COUNT(*) FROM daily_prices p WHERE p.series_id=s.id) AS sessions, "
            "(SELECT MIN(p.session_date) FROM daily_prices p WHERE p.series_id=s.id) AS first_session, "
            "(SELECT MAX(p.session_date) FROM daily_prices p WHERE p.series_id=s.id) AS last_session "
            "FROM series s ORDER BY s.symbol, s.provider"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError("Could not read local instrument catalog") from exc
    finally:
        conn.close()


def read_daily(
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 250,
    db: Path | None = None,
    latest: bool = False,
    provider: str = "alpha_vantage",
) -> dict[str, object] | None:
    """Bounded, chronological read of stored daily prices; no live provider calls."""
    if limit < 1 or limit > 4000:
        raise ValueError("Daily price limit must be 1–4000")
    if provider not in ("alpha_vantage", "alpaca_sip", "tiingo"):
        raise ValueError("Invalid historical data provider")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return None
    try:
        series = conn.execute(
            "SELECT id, symbol, provider, frequency, adjustment, data_kind "
            "FROM series WHERE symbol=? AND provider=? "
            "AND frequency='1d' AND adjustment='raw_unadjusted'",
            (symbol.upper(), provider),
        ).fetchone()
        if series is None:
            return None
        clauses = ["series_id=?"]
        params: list[str | int] = [series["id"]]
        if start is not None:
            clauses.append("session_date>=?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("session_date<=?")
            params.append(end.isoformat())
        query = (
            "SELECT session_date, open, high, low, close, volume "
            "FROM daily_prices WHERE "
            + " AND ".join(clauses)
            + (
                " ORDER BY session_date DESC LIMIT ?"
                if latest
                else " ORDER BY session_date ASC LIMIT ?"
            )
        )
        rows = conn.execute(query, (*params, limit + 1)).fetchall()
        shown = rows[:limit]
        if latest:
            shown = list(reversed(shown))
        recent = conn.execute(
            "SELECT retrieved_at, provider_last_refreshed, source_url, provider_terms_url "
            "FROM ingestion_runs WHERE series_id=? "
            "ORDER BY retrieved_at DESC, id DESC LIMIT 1",
            (series["id"],),
        ).fetchone()
        return {
            "symbol": series["symbol"],
            "provider": series["provider"],
            "frequency": series["frequency"],
            "price_adjustment": series["adjustment"],
            "data_kind": series["data_kind"],
            "source_url": recent["source_url"] if recent else None,
            "provider_terms_url": recent["provider_terms_url"] if recent else None,
            "retrieved_at": recent["retrieved_at"] if recent else None,
            "provider_last_refreshed": recent["provider_last_refreshed"] if recent else None,
            "has_more": len(rows) > limit,
            "observations": [dict(row) for row in shown],
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not read local historical prices") from exc
    finally:
        conn.close()


def list_imports(*, db: Path | None = None, limit: int = 25) -> list[dict[str, object]]:
    if limit < 1 or limit > 100:
        raise ValueError("Import history limit must be 1–100")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT r.id, s.symbol, s.provider, r.retrieved_at, r.imported_at, "
            "r.payload_sha256, r.first_session, r.last_session, r.observations, "
            "r.inserted, r.updated, r.unchanged, r.stale, r.source_url "
            "FROM ingestion_runs r JOIN series s ON r.series_id=s.id "
            "ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError("Could not read local import history") from exc
    finally:
        conn.close()


def search_import_history(
    *,
    db: Path | None = None,
    query: str = "",
    provider: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """Filter all persisted ingestion runs before applying SQLite pagination."""
    if not 1 <= limit <= 100 or offset < 0 or len(query) > 80 or len(provider) > 40:
        raise ValueError("Invalid import-history search parameters")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    try:
        catalog_available = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbol_catalog'"
            ).fetchone()
            is not None
        )
        terms = ["1=1"]
        params: list[object] = []
        if provider:
            terms.append("s.provider = ?")
            params.append(provider)
        if query.strip():
            pattern = (
                "%"
                + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            matches = [
                "s.symbol LIKE ? ESCAPE '\\'",
                "s.provider LIKE ? ESCAPE '\\'",
            ]
            params.extend([pattern, pattern])
            if catalog_available:
                matches.append(
                    "EXISTS (SELECT 1 FROM symbol_catalog c WHERE c.symbol=s.symbol "
                    "AND c.name LIKE ? ESCAPE '\\')"
                )
                params.append(pattern)
            terms.append("(" + " OR ".join(matches) + ")")
        joins = "FROM ingestion_runs r JOIN series s ON r.series_id=s.id"
        where = " WHERE " + " AND ".join(terms)
        total = conn.execute("SELECT COUNT(*) " + joins + where, params).fetchone()[0]
        rows = conn.execute(
            "SELECT r.id, s.symbol, s.provider, r.retrieved_at, r.imported_at, "
            "r.payload_sha256, r.first_session, r.last_session, r.observations, "
            "r.inserted, r.updated, r.unchanged, r.stale, r.source_url "
            + joins
            + where
            + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not search local import history") from exc
    finally:
        conn.close()


def search_symbols(
    *,
    query: str = "",
    asset_type: str | None = None,
    limit: int = 50,
    db: Path | None = None,
) -> dict[str, object]:
    """Initialize the durable local catalog on first search, then read it."""
    if limit < 1 or limit > 100:
        raise ValueError("Symbol search limit must be 1–100")
    if asset_type not in (None, *ASSET_TYPES):
        raise ValueError("Invalid asset-type filter")
    if len(query) > 80:
        raise ValueError("Search query is too long")
    path = db if db is not None else database_path()
    # First-run creation and legacy-schema migration are short serialized writes.
    # Normal catalog reads remain independent of backups, restores and other writers.
    _ensure_symbol_catalog(path)
    conn = _read_connection(path)
    if conn is None:
        raise StorageError("Could not open the newly initialized symbol catalog")
    try:
        count = conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0]
        source_row = conn.execute(
            "SELECT source, MAX(updated_at) AS updated_at FROM symbol_catalog "
            "GROUP BY source ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        pattern = (
            "%"
            + query.strip().upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        prefix = query.strip().upper() + "%"
        records = conn.execute(
            "SELECT symbol, name, asset_type, exchange FROM symbol_catalog "
            "WHERE (? IS NULL OR asset_type=?) "
            "AND (symbol LIKE ? ESCAPE '\\' OR UPPER(name) LIKE ? ESCAPE '\\') "
            "ORDER BY CASE WHEN symbol LIKE ? THEN 0 ELSE 1 END, symbol LIMIT ?",
            (asset_type, asset_type, pattern, pattern, prefix, limit),
        ).fetchall()
        return {
            "source": source_row["source"] if source_row else "starter",
            "total": count,
            "updated_at": source_row["updated_at"] or None if source_row else None,
            "results": [dict(row) for row in records],
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not read the local symbol catalog") from exc
    finally:
        conn.close()


def lookup_symbol_type(symbol: str, *, db: Path | None = None) -> str | None:
    """Find the instrument class before attempting a provider-price request."""
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return next(
            (row["asset_type"] for row in STARTER_SYMBOLS if row["symbol"] == symbol),
            None,
        )
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return next(
                (row["asset_type"] for row in STARTER_SYMBOLS if row["symbol"] == symbol),
                None,
            )
        result = conn.execute(
            "SELECT asset_type FROM symbol_catalog WHERE symbol=?", (symbol,)
        ).fetchone()
        if result:
            return result["asset_type"]
        # Protect direct API calls against unsupported reference instruments
        # even if an earlier v2 catalog has not yet been upgraded by the UI.
        return next(
            (
                row["asset_type"]
                for row in STARTER_SYMBOLS
                if row["symbol"] == symbol and row["asset_type"] in ("Index", "Mutual Fund")
            ),
            None,
        )
    except sqlite3.Error as exc:
        raise StorageError("Could not check symbol asset type") from exc
    finally:
        conn.close()


@serialized_storage_write
def replace_symbol_catalog(
    records: list[dict[str, str]], *, db: Path | None = None
) -> dict[str, object]:
    """Atomically replace only the symbol *directory*, never daily price rows."""
    if not records:
        raise StorageError("Refusing an empty symbol catalog")
    rows, timestamp = synchronized_rows(records)
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            conn.execute("DELETE FROM symbol_catalog WHERE asset_type IN ('Stock', 'ETF')")
            conn.executemany(
                "INSERT INTO symbol_catalog"
                "(symbol, name, asset_type, exchange, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        total = conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0]
        return {
            "status": "synced",
            "total": total,
            "updated_at": timestamp,
            "source": "alpha_vantage",
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not update the local symbol catalog") from exc
    finally:
        conn.close()


@serialized_storage_write
def append_action_event(
    *,
    symbol: str,
    provider: str,
    status: str,
    duration_ms: int,
    import_id: int | None = None,
    error_code: str | None = None,
    db: Path | None = None,
) -> int:
    """Append restricted, credential-free acquisition metadata to local SQLite."""
    if status not in ("success", "failed") or not symbol or not provider:
        raise ValueError("Invalid action event")
    if duration_ms < 0 or (status == "success" and import_id is None):
        raise ValueError("Invalid action result")
    if error_code is not None and (len(error_code) > 64 or not error_code.isidentifier()):
        raise ValueError("Invalid error code")
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO action_events(occurred_at,action,status,symbol,provider,import_id,error_code,duration_ms) "
                "VALUES (?, 'acquisition', ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                    status,
                    symbol,
                    provider,
                    import_id,
                    error_code,
                    duration_ms,
                ),
            )
        return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise StorageError("Unable to record local action event") from exc
    finally:
        conn.close()


def list_action_events(*, limit: int = 100, db: Path | None = None) -> list[dict[str, object]]:
    """Read bounded operational events; an older database has no recorded events."""
    if limit < 1 or limit > 100:
        raise ValueError("Event history limit must be 1–100")
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return []
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            return []
        rows = conn.execute(
            "SELECT id,occurred_at,action,status,symbol,provider,import_id,error_code,duration_ms "
            "FROM action_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise StorageError("Unable to read local action history") from exc
    finally:
        conn.close()
