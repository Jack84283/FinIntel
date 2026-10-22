"""Transactional, versioned SQLite store for historical end-of-day price series.

Only validated HistoricalBatch objects can be imported. API reads never initialize
the database and cannot modify it. PR #3/M1 data persistence (GitHub PR #4).
"""

import hashlib
import os
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from finintel.market_data.models import HistoricalBatch
from finintel.market_data.symbol_catalog import STARTER_SYMBOLS, search_starter, synchronized_rows

SCHEMA_VERSION = 2
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


class StorageError(Exception):
    """Safe failure message for local CLI or API consumers."""


def database_path() -> Path:
    """Use a single local database path for the CLI and development backend."""
    return Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3")).expanduser()


def _ensure_version(conn: sqlite3.Connection, *, allow_missing: bool) -> bool:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and allow_missing:
        return False
    if version not in (1, SCHEMA_VERSION):
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
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V1 + "\nPRAGMA user_version = 1;\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StorageError("Unable to initialize the SQLite schema") from exc
        version = 1
    if version != 1:
        raise StorageError("No migration exists for this SQLite schema version")
    # Explicit 1 -> 2 migration: existing prices and ingestion runs are untouched.
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V2)
        conn.executemany(
            "INSERT INTO symbol_catalog(symbol, name, asset_type, exchange, source, updated_at) "
            "VALUES (?, ?, ?, ?, 'starter', '')",
            (
                (item["symbol"], item["name"], item["asset_type"], item["exchange"])
                for item in STARTER_SYMBOLS
            ),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise StorageError("Unable to migrate the SQLite symbol catalog") from exc


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
        return conn
    except (sqlite3.Error, StorageError) as exc:
        conn.close()
        raise StorageError("Local database is unavailable or needs migration") from exc


def _safe_source(batch: HistoricalBatch) -> None:
    """Prevent credentials and unwanted remote schemes from entering audit history."""
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(batch.source_url)
    params = parse_qs(url.query)
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
    ):
        raise StorageError("Snapshot source metadata is not an approved sanitized provider URL")
    terms = urlsplit(batch.provider_terms_url)
    if terms.scheme != "https" or terms.hostname != "www.alphavantage.co":
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


def list_instruments(db: Path | None = None) -> list[dict[str, object]]:
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT s.symbol, s.provider, s.frequency, s.adjustment, s.data_kind, "
            "COUNT(p.session_date) AS sessions, MIN(p.session_date) AS first_session, "
            "MAX(p.session_date) AS last_session "
            "FROM series s LEFT JOIN daily_prices p ON s.id=p.series_id "
            "GROUP BY s.id ORDER BY s.symbol, s.provider"
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
) -> dict[str, object] | None:
    """Bounded, chronological read of stored daily prices; no live provider calls."""
    if limit < 1 or limit > 1000:
        raise ValueError("Daily price limit must be 1–1000")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        return None
    try:
        series = conn.execute(
            "SELECT id, symbol, provider, frequency, adjustment, data_kind "
            "FROM series WHERE symbol=? AND provider='alpha_vantage' "
            "AND frequency='1d' AND adjustment='raw_unadjusted'",
            (symbol.upper(),),
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


def search_symbols(
    *,
    query: str = "",
    asset_type: str | None = None,
    limit: int = 50,
    db: Path | None = None,
) -> dict[str, object]:
    """Read-only bounded catalog search, even on legacy v1 or fresh installs."""
    if limit < 1 or limit > 100:
        raise ValueError("Symbol search limit must be 1–100")
    if asset_type not in (None, "Stock", "ETF"):
        raise ValueError("Invalid asset-type filter")
    if len(query) > 80:
        raise ValueError("Search query is too long")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        data = search_starter(query, asset_type, limit)
        return {"source": "starter", "total": len(STARTER_SYMBOLS), "updated_at": None, "results": data}
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
            data = search_starter(query, asset_type, limit)
            return {
                "source": "starter",
                "total": len(STARTER_SYMBOLS),
                "updated_at": None,
                "results": data,
            }
        count = conn.execute("SELECT COUNT(*) FROM symbol_catalog").fetchone()[0]
        source_row = conn.execute(
            "SELECT source, MAX(updated_at) AS updated_at FROM symbol_catalog "
            "GROUP BY source ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        pattern = "%" + query.strip().upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
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
            conn.execute("DELETE FROM symbol_catalog")
            conn.executemany(
                "INSERT INTO symbol_catalog"
                "(symbol, name, asset_type, exchange, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return {"status": "synced", "total": len(rows), "updated_at": timestamp, "source": "alpha_vantage"}
    except sqlite3.Error as exc:
        raise StorageError("Could not update the local symbol catalog") from exc
    finally:
        conn.close()
