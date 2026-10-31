"""FRED daily *closing levels* of benchmark indexes; no synthetic OHLC or volume.

FRED distributes S&P/Dow daily close series with a limited history window.
Each record retains the original FRED series URL for local provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx

from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderRateLimited,
    ProviderUnavailable,
)
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
INDEX_CAPABILITY_METADATA = {
    "SP500": {
        "series_type": "price",
        "frequency": "daily",
        "currency": None,
        "units": "index_points",
        "licensing_note": "FRED terms apply; verify permitted use before redistribution.",
        "known_history_limit": "FRED determines the available daily observation window; no local history cap.",
    },
    "DJIA": {
        "series_type": "price",
        "frequency": "daily",
        "currency": None,
        "units": "index_points",
        "licensing_note": "FRED terms apply; verify permitted use before redistribution.",
        "known_history_limit": "FRED determines the available daily observation window; no local history cap.",
    },
    "NASDAQCOM": {
        "series_type": "price",
        "frequency": "daily",
        "currency": None,
        "units": "index_points",
        "licensing_note": "FRED terms apply; verify permitted use before redistribution.",
        "known_history_limit": "FRED determines the available daily observation window; no local history cap.",
    },
    "NDX": {
        "series_type": "price",
        "frequency": "daily",
        "currency": None,
        "units": "index_points",
        "licensing_note": "FRED terms apply; verify permitted use before redistribution.",
        "known_history_limit": "FRED determines the available daily observation window; no local history cap.",
    },
    "VIX": {
        "series_type": "volatility",
        "frequency": "daily",
        "currency": None,
        "units": "index_points",
        "licensing_note": "FRED terms apply; verify permitted use before redistribution.",
        "known_history_limit": "FRED determines the available daily observation window; no local history cap.",
    },
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


def _validate_date_range(start: date | None, end: date | None) -> None:
    today = datetime.now(UTC).date()
    if start is not None and start > today or end is not None and end > today:
        raise IndexHistoryError("Index history dates cannot be in the future")
    if start is not None and end is not None and start > end:
        raise IndexHistoryError("Index history start date must not be after the end date")


def index_request_url(code: str, start: date | None = None, end: date | None = None) -> str:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    _validate_date_range(start, end)
    params = {"id": INDEX_SERIES[code][1]}
    if start is not None:
        params["cosd"] = start.isoformat()
    if end is not None:
        params["coed"] = end.isoformat()
    return FRED_CSV + "?" + urlencode(params)


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


def fetch_fred_index(
    code: str,
    *,
    client: httpx.Client | None = None,
    request_guard: Callable[[], None] | None = None,
    start: date | None = None,
    end: date | None = None,
    payload_observer: Callable[[bytes], None] | None = None,
) -> list[tuple[str, str]]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    _validate_date_range(start, end)
    own = client is None
    session = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        if request_guard is not None:
            request_guard()
        response = session.get(
            FRED_CSV,
            params={
                key: value
                for key, value in (
                    ("id", INDEX_SERIES[code][1]),
                    ("cosd", start.isoformat() if start is not None else None),
                    ("coed", end.isoformat() if end is not None else None),
                )
                if value is not None
            },
            headers={"Accept": "text/csv"},
        )
        if response.status_code == 429:
            raise ProviderRateLimited("FRED request quota reached")
        if response.status_code in (401, 403):
            raise ProviderAccessDenied("FRED rejected the index request")
        if response.status_code >= 500:
            raise ProviderUnavailable("FRED index history is temporarily unavailable")
        if response.status_code != 200:
            raise IndexHistoryError("FRED index history is temporarily unavailable")
        if payload_observer is not None:
            payload_observer(response.content)
        rows = parse_fred_csv(code, response.content)
        if start is not None and rows[0][0] < start.isoformat():
            raise IndexHistoryError("FRED returned observations before the requested start date")
        if end is not None and rows[-1][0] > end.isoformat():
            raise IndexHistoryError("FRED returned observations after the requested end date")
        return rows
    except httpx.RequestError as exc:
        raise ProviderUnavailable("Could not contact FRED for index history") from exc
    finally:
        if own:
            session.close()


@serialized_storage_write
def store_index_closes(
    code: str,
    observations: list[tuple[str, str]],
    *,
    db: Path | None = None,
    requested_start: date | None = None,
    requested_end: date | None = None,
    raw_payload: bytes | None = None,
) -> dict[str, object]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    _validate_date_range(requested_start, requested_end)
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
    if requested_start is not None and values[0][0] < requested_start.isoformat():
        raise IndexHistoryError("Index observations start before the requested range")
    if requested_end is not None and values[-1][0] > requested_end.isoformat():
        raise IndexHistoryError("Index observations end after the requested range")
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        url = index_source_url(code)
        request_url = index_request_url(code, requested_start, requested_end)
        observation_revision = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        payload_revision = (
            hashlib.sha256(raw_payload).hexdigest() if raw_payload is not None else None
        )
        inserted = updated = unchanged = 0
        with conn:
            ingestion_id = conn.execute(
                "INSERT INTO index_ingestion_runs("
                "index_code,provider,requested_start,requested_end,retrieved_at,source_url,"
                "observation_sha256,payload_sha256,first_session,last_session,observations,"
                "inserted,updated,unchanged) "
                "VALUES (?, 'fred', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
                (
                    code,
                    requested_start.isoformat() if requested_start is not None else None,
                    requested_end.isoformat() if requested_end is not None else None,
                    now,
                    request_url,
                    observation_revision,
                    payload_revision,
                    values[0][0],
                    values[-1][0],
                    len(values),
                ),
            ).lastrowid
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
            conn.execute(
                "UPDATE index_ingestion_runs SET inserted=?,updated=?,unchanged=? WHERE id=?",
                (inserted, updated, unchanged, ingestion_id),
            )
            coverage = conn.execute(
                "SELECT MIN(session_date),MAX(session_date) FROM index_daily_closes "
                "WHERE index_code=? AND provider='fred'",
                (code,),
            ).fetchone()
            conn.execute(
                "UPDATE index_series_catalog SET local_first_session=?,local_last_session=?,"
                "coverage_updated_at=? WHERE index_code=? AND provider='fred'",
                (coverage[0], coverage[1], now, code),
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
            "request_url": request_url,
            "observation_sha256": observation_revision,
            "payload_sha256": payload_revision,
            "request_start": requested_start.isoformat() if requested_start else None,
            "request_end": requested_end.isoformat() if requested_end else None,
        }
    except sqlite3.Error as exc:
        raise StorageError("Could not store local index closing history") from exc
    finally:
        conn.close()


def acquire_index_closes(
    code: str,
    *,
    db: Path | None = None,
    client: httpx.Client | None = None,
    request_guard: Callable[[], None] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, object]:
    # Explicit user action only. No periodic downloads.
    if request_guard is None:
        from finintel.market_data.daily_acquisition_store import provider_request_guard

        request_guard = provider_request_guard("fred", db=db)
    payloads: list[bytes] = []
    try:
        result = store_index_closes(
            code,
            fetch_fred_index(
                code,
                client=client,
                request_guard=request_guard,
                start=start,
                end=end,
                payload_observer=payloads.append,
            ),
            db=db,
            requested_start=start,
            requested_end=end,
            raw_payload=payloads[0] if payloads else None,
        )
    except (ProviderRateLimited, ProviderAccessDenied) as exc:
        from finintel.market_data.daily_acquisition_store import mark_provider_status

        mark_provider_status(
            "fred",
            "quota_exhausted" if isinstance(exc, ProviderRateLimited) else "auth_failed",
            str(exc),
            db=db,
        )
        raise
    from finintel.market_data.daily_acquisition_store import mark_provider_status

    mark_provider_status("fred", "available", db=db)
    return result


def index_catalog(*, db: Path | None = None) -> list[dict[str, object]]:
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    try:
        stored: dict[str, dict[str, object]] = {}
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn is not None else 0
        if conn is not None and version >= 11:
            catalog = []
            for row in conn.execute(
                "SELECT c.index_code AS code,c.official_name AS name,"
                "c.provider,c.source_url,c.series_type,"
                "c.frequency,c.currency,c.units,c.licensing_note,c.known_history_limit,"
                "COUNT(d.session_date) AS sessions,c.local_first_session,"
                "c.local_last_session,c.provider_earliest_available,"
                "c.provider_latest_available,c.coverage_updated_at,"
                "'daily_close_only' AS data_kind "
                "FROM index_series_catalog c LEFT JOIN index_daily_closes d "
                "ON d.index_code=c.index_code AND d.provider=c.provider "
                "GROUP BY c.index_code,c.provider ORDER BY CASE c.index_code "
                "WHEN 'SP500' THEN 1 WHEN 'DJIA' THEN 2 WHEN 'NASDAQCOM' THEN 3 "
                "WHEN 'NDX' THEN 4 WHEN 'VIX' THEN 5 ELSE 99 END"
            ):
                item = dict(row)
                item["fred_series"] = INDEX_SERIES[item["code"]][1]
                item["first_session"] = item["local_first_session"]
                item["last_session"] = item["local_last_session"]
                item["data_kind"] = "daily_close_only"
                catalog.append(item)
            return catalog
        if conn is not None and version >= 7:
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
                "local_first_session": stored[code]["first_session"] if code in stored else None,
                "local_last_session": stored[code]["last_session"] if code in stored else None,
                "provider_earliest_available": None,
                "provider_latest_available": None,
                **INDEX_CAPABILITY_METADATA[code],
                "coverage_updated_at": None,
                "data_kind": "daily_close_only",
            }
            for code, (name, series) in INDEX_SERIES.items()
        ]
    finally:
        if conn is not None:
            conn.close()


def read_index_closes(
    code: str,
    *,
    limit: int = 4000,
    start: date | None = None,
    end: date | None = None,
    db: Path | None = None,
) -> dict[str, object]:
    if code not in INDEX_SERIES:
        raise IndexHistoryError("Unsupported index code")
    if not 1 <= limit <= 10000:
        raise IndexHistoryError("Index history limit must be 1–10000")
    _validate_date_range(start, end)
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    try:
        rows = []
        if conn is not None and conn.execute("PRAGMA user_version").fetchone()[0] >= 7:
            clauses = ["index_code=?", "provider='fred'"]
            params: list[object] = [code]
            if start is not None:
                clauses.append("session_date>=?")
                params.append(start.isoformat())
            if end is not None:
                clauses.append("session_date<=?")
                params.append(end.isoformat())
            rows = conn.execute(
                "SELECT session_date,close FROM index_daily_closes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY session_date DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
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
