"""Bounded read-only analytical access to stored and explicitly identified series."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from finintel.market_data.analytics import (
    MAX_OBSERVATIONS,
    AnalyticsError,
    SeriesIdentity,
    compare_series,
    compute_series,
    validated_observations,
)
from finintel.market_data.index_history import INDEX_SERIES
from finintel.storage.db import StorageError, _read_connection, database_path


def _read_series(
    conn: sqlite3.Connection,
    *,
    kind: str,
    code: str,
    provider: str,
    start: date,
    end: date,
) -> tuple[SeriesIdentity, list[tuple[str, str]], dict]:
    if kind == "index":
        if code not in INDEX_SERIES or provider != "fred":
            raise AnalyticsError("Unsupported stored index source")
        row = conn.execute(
            "SELECT series_type,frequency,units,source_url,coverage_updated_at "
            "FROM index_series_catalog WHERE index_code=? AND provider=?",
            (code, provider),
        ).fetchone()
        if row is None or row["units"] != "index_points" or row["frequency"] != "daily":
            raise AnalyticsError("Index return semantics or source metadata are unavailable")
        semantics = "volatility_index" if row["series_type"] == "volatility" else "price_index"
        identity = SeriesIdentity(
            kind, code, provider, semantics, semantics, "daily", "index_points"
        )
        sql = (
            "SELECT session_date,close,retrieved_at FROM index_daily_closes "
            "WHERE index_code=? AND provider=? AND session_date>=? AND session_date<=? "
            "ORDER BY session_date LIMIT ?"
        )
        args = (code, provider, start.isoformat(), end.isoformat(), MAX_OBSERVATIONS + 1)
        provenance = {
            "source_url": row["source_url"],
            "coverage_updated_at": row["coverage_updated_at"],
        }
    elif kind == "equity":
        if not code or len(code) > 20 or not code.replace("-", "").replace(".", "").isalnum():
            raise AnalyticsError("Invalid equity instrument")
        row = conn.execute(
            "SELECT id,adjustment,frequency FROM series "
            "WHERE symbol=? AND provider=? AND adjustment='raw_unadjusted' AND frequency='1d'",
            (code, provider),
        ).fetchone()
        if row is None:
            raise AnalyticsError("No supported raw daily equity series for this source")
        identity = SeriesIdentity(
            kind,
            code,
            provider,
            "raw_unadjusted",
            "raw_unadjusted_equity_price_usd_per_share",
            "1d",
            "usd_per_share",
        )
        sql = (
            "SELECT session_date,close,retrieved_at FROM daily_prices "
            "WHERE series_id=? AND session_date>=? AND session_date<=? "
            "ORDER BY session_date LIMIT ?"
        )
        args = (row["id"], start.isoformat(), end.isoformat(), MAX_OBSERVATIONS + 1)
        provenance = {"source_url": None, "coverage_updated_at": None}
    else:
        raise AnalyticsError("Only index or equity source observations are supported")
    records = conn.execute(sql, args).fetchall()
    if len(records) > MAX_OBSERVATIONS:
        raise AnalyticsError("Window exceeds 10,000 observations; choose a shorter date range")
    rows = [(record["session_date"], record["close"]) for record in records]
    hasher = hashlib.sha256()
    # Hash the exact semantic identity and source snapshot, not private URLs or credentials.
    for field in (kind, code, provider, identity.adjustment, identity.return_semantics):
        hasher.update(field.encode("utf-8"))
        hasher.update(b"\0")
    for record in records:
        for value in (record["session_date"], record["close"], record["retrieved_at"]):
            hasher.update(str(value).encode("utf-8"))
            hasher.update(b"\0")
    provenance["snapshot_sha256"] = hasher.hexdigest()
    return identity, rows, provenance


def _quality_status(conn: sqlite3.Connection, codes: list[str]) -> dict:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('data_quality_scan_progress','data_quality_findings','data_quality_source_state')"
        )
    }
    if len(tables) < 3:
        return {"status": "unknown", "quality_verified": False, "active_unresolved_errors": None}
    scan = conn.execute(
        "SELECT status,stale,source_data_version_end,validation_reference_date "
        "FROM data_quality_scan_progress WHERE scan_key='all'"
    ).fetchone()
    source = conn.execute(
        "SELECT source_version FROM data_quality_source_state WHERE id=1"
    ).fetchone()
    bad = conn.execute(
        "SELECT COUNT(*) FROM data_quality_findings "
        "WHERE code IN (" + ",".join("?" for _ in codes) + ") "
        "AND superseded_at IS NULL AND severity='error' "
        "AND review_state NOT IN ('rejected','accepted_correction')",
        codes,
    ).fetchone()[0]
    current = datetime.now(UTC).date().isoformat()
    complete = bool(
        scan is not None
        and source is not None
        and scan["status"] == "completed"
        and not scan["stale"]
        and scan["source_data_version_end"] == source["source_version"]
        and scan["validation_reference_date"] == current
    )
    return {
        "status": "complete" if complete else "stale_or_partial",
        "quality_verified": complete and bad == 0,
        "active_unresolved_errors": bad,
        "scan_complete_current": complete,
    }


def run_analytics(
    *,
    kind: str = "index",
    code: str = "SP500",
    provider: str = "fred",
    start: date,
    end: date,
    window: int = 20,
    comparison_code: str | None = None,
    comparison_kind: str = "index",
    comparison_provider: str = "fred",
    missing_policy: str = "intersection",
    quality_policy: str = "require_complete",
    allow_volatility_returns: bool = False,
    db: Path | None = None,
) -> dict:
    """No writes, migrations, network fetches, provider fallback or silent truncation."""
    if start > end or end > datetime.now(UTC).date():
        raise AnalyticsError("Invalid or future analytical date range")
    if quality_policy not in {"require_complete", "allow_unverified"}:
        raise AnalyticsError("Unsupported quality policy")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        raise AnalyticsError("No local stored observations are available")
    try:
        identity, rows, provenance = _read_series(
            conn, kind=kind, code=code, provider=provider, start=start, end=end
        )
        comparison = None
        other_rows = []
        other_provenance = None
        codes = [code]
        if comparison_code:
            comparison, other_rows, other_provenance = _read_series(
                conn,
                kind=comparison_kind,
                code=comparison_code,
                provider=comparison_provider,
                start=start,
                end=end,
            )
            codes.append(comparison_code)
        quality = _quality_status(conn, codes)
        if quality_policy == "require_complete" and not quality["quality_verified"]:
            raise AnalyticsError(
                "Current complete quality scan with no unresolved errors required; "
                "rerun quality validation or explicitly allow_unverified"
            )
        records = validated_observations(rows)
        result = compute_series(
            identity, records, window=window, allow_volatility_returns=allow_volatility_returns
        )
        result.update(
            {
                "request": {"start": start.isoformat(), "end": end.isoformat()},
                "source": provenance,
                "quality": quality,
                "quality_policy": quality_policy,
                "unverified_data": not quality["quality_verified"],
                "read_only": True,
                "no_network": True,
                "no_synthetic_sessions": True,
            }
        )
        if comparison:
            other = validated_observations(other_rows)
            result["comparison"] = compare_series(
                identity,
                records,
                comparison,
                other,
                window=window,
                missing_policy=missing_policy,
            )
            result["comparison"]["source"] = other_provenance
        return result
    except sqlite3.Error as exc:
        raise StorageError("Local analytics query failed") from exc
    finally:
        conn.close()
