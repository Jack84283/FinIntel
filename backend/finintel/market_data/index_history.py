"""FRED daily *closing levels* of benchmark indexes; no synthetic OHLC or volume.

FRED distributes S&P/Dow daily close series with a limited history window.
Each record retains the original FRED series URL for local provenance.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from finintel.storage.db import (
    StorageError,
    _read_connection,
    _write_connection,
    database_path,
    serialized_storage_write,
)

# FRED series identifiers are allowlisted, never supplied as arbitrary URLs.
INDEX_SERIES = {
    "SP500": ("S&P 500", "SP500"),
    "DJIA": ("Dow Jones Industrial Average", "DJIA"),
    "NASDAQCOM": ("Nasdaq Composite", "NASDAQCOM"),
    "NDX": ("Nasdaq-100", "NASDAQ100"),
    "VIX": ("Cboe VIX", "VIXCLS"),
}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MAX_DOWNLOAD_BYTES = 2_000_000
MAX_ROWS = 25_000


class IndexHistoryError(ValueError):
    """Provider content is invalid or cannot be used safely."""


def index_source_url(code: str) -> str:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    return "https://fred.stlouisfed.org/series/" + INDEX_SERIES[code][1]


def parse_fred_csv(code: str, payload: bytes) -> list[tuple[str, str]]:
    """Validate all observations before storing any; missing holidays are omitted."""
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    if not payload or len(payload) > MAX_DOWNLOAD_BYTES:
        raise IndexHistoryError("Index history download is empty or exceeds size limit")
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        series_id = INDEX_SERIES[code][1]
        if reader.fieldnames not in (
            ["observation_date", series_id],
            ["DATE", series_id],
        ):
            raise IndexHistoryError("Unexpected FRED CSV columns")
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in reader:
            if len(rows) >= MAX_ROWS or None in row:
                raise IndexHistoryError("Index history CSV has malformed rows")
            observed = row.get("observation_date") or row.get("DATE")
            raw = row.get(series_id)
            if not isinstance(observed, str) or not isinstance(raw, str):
                raise IndexHistoryError("Index history CSV has missing values")
            try:
                when = date.fromisoformat(observed)
            except ValueError as exc:
                raise IndexHistoryError("Invalid index observation date") from exc
            if when.isoformat() != observed or when > datetime.now(UTC).date():
                raise IndexHistoryError("Index history contains invalid or future dates")
            if observed in seen or (rows and observed <= rows[-1][0]):
                raise IndexHistoryError("Index history must have unique ascending dates")
            seen.add(observed)
            if raw.strip() in ("", "."):
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise IndexHistoryError("Invalid index closing level") from exc
            if not value.is_finite() or value <= 0:
                raise IndexHistoryError("Index levels must be finite positive numbers")
            rows.append((observed, str(value)))
        if not rows:
            raise IndexHistoryError("Index series has no valid daily closing levels")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise IndexHistoryError("Index history CSV cannot be decoded") from exc


def fetch_fred_index(code: str, *, client: httpx.Client | None = None) -> list[tuple[str, str]]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    own = client is None
    session = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = session.get(
            FRED_CSV,
            params={"id": INDEX_SERIES[code][1]},
            headers={"Accept": "text/csv"},
        )
        if response.status_code != 200:
            raise IndexHistoryError("FRED index history is temporarily unavailable")
        return parse_fred_csv(code, response.content)
    except httpx.RequestError as exc:
        raise IndexHistoryError("Could not contact FRED for index history") from exc
    finally:
        if own:
            session.close()


@serialized_storage_write
def store_index_closes(
    code: str, observations: list[tuple[str, str]], *, db: Path | None = None
) -> dict[str, object]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    if not observations or len(observations) > MAX_ROWS:
        raise IndexHistoryError("Invalid number of index observations")
    # Same strict validation for live downloads and programmatic imports.
    raw = (
        "observation_date,"
        + INDEX_SERIES[code][1]
        + "\n"
        + "".join(day + "," + level + "\n" for day, level in observations)
    )
    values = parse_fred_csv(code, raw.encode("utf-8"))
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        now = datetime.now(UTC).isoformat()
        url = index_source_url(code)
        inserted = updated = unchanged = 0
        with conn:
            for day, level in values:
                existing = conn.execute(
                    "SELECT close FROM index_daily_closes "
                    "WHERE index_code=? AND provider='fred' AND session_date=?",
                    (code, day),
                ).fetchone()
                if existing is None:
                    inserted += 1
                elif Decimal(existing["close"]) != Decimal(level):
                    updated += 1
                else:
                    unchanged += 1
                    continue
                conn.execute(
                    "INSERT INTO index_daily_closes "
                    "(index_code,provider,session_date,close,retrieved_at,source_url) "
                    "VALUES (?,'fred',?,?,?,?) "
                    "ON CONFLICT(index_code,provider,session_date) DO UPDATE SET "
                    "close=excluded.close,retrieved_at=excluded.retrieved_at,"
                    "source_url=excluded.source_url",
                    (code, day, level, now, url),
                )
        return {
            "code": code,
            "provider": "fred",
            "status": "stored",
            "sessions": len(values),
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "first_session": values[0][0],
            "last_session": values[-1][0],
            "source_url": url,
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not store local index closing history") from exc
    finally:
        conn.close()


def acquire_index_closes(
    code: str, *, db: Path | None = None, client: httpx.Client | None = None
) -> dict[str, object]:
    # Explicit user action only. No periodic downloads.
    return store_index_closes(code, fetch_fred_index(code, client=client), db=db)


def index_catalog(*, db: Path | None = None) -> list[dict[str, object]]:
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    try:
        stored: dict[str, dict[str, object]] = {}
        if conn is not None and conn.execute("PRAGMA user_version").fetchone()[0] >= 7:
            for row in conn.execute(
                "SELECT index_code,COUNT(*) AS sessions,MIN(session_date) AS first_session,"
                "MAX(session_date) AS last_session FROM index_daily_closes "
                "WHERE provider='fred' GROUP BY index_code"
            ):
                stored[row["index_code"]] = dict(row)
        return [
            {
                "code": code,
                "name": name,
                "fred_series": series,
                "provider": "fred",
                "source_url": index_source_url(code),
                "sessions": int(stored[code]["sessions"]) if code in stored else 0,
                "first_session": stored[code]["first_session"] if code in stored else None,
                "last_session": stored[code]["last_session"] if code in stored else None,
                "data_kind": "daily_close_only",
            }
            for code, (name, series) in INDEX_SERIES.items()
        ]
    finally:
        if conn is not None:
            conn.close()


def read_index_closes(code: str, *, limit: int = 4000, db: Path | None = None) -> dict[str, object]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    if not 1 <= limit <= 10000:
        raise IndexHistoryError("Index history limit must be 1–10000")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    try:
        rows = (
            conn.execute(
                "SELECT session_date,close FROM index_daily_closes WHERE index_code=? "
                "AND provider='fred' ORDER BY session_date DESC LIMIT ?",
                (code, limit + 1),
            ).fetchall()
            if conn is not None and conn.execute("PRAGMA user_version").fetchone()[0] >= 7
            else []
        )
        return {
            "code": code,
            "name": INDEX_SERIES[code][0],
            "data_kind": "daily_close_only",
            "unit": "index_points",
            "provider": "fred",
            "source_url": index_source_url(code),
            "has_more": len(rows) > limit,
            "observations": [dict(row) for row in reversed(rows[:limit])],
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not read stored index levels") from exc
    finally:
        if conn is not None:
            conn.close()
