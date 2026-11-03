"""Evidence-labeled, point-in-time pairwise index constituent overlap.

No inference from prices, current symbols or ETF holdings. Both dated source
snapshots must exist before an overlap (including zero overlap) can be claimed.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from finintel.market_data.membership import (
    INDEX_NAMES,
    _as_of,
    _authoritative_snapshot,
    _latest,
    _matching_pattern,
)
from finintel.storage.db import StorageError, _read_connection, database_path


def constituent_overlap(
    left_code: str,
    right_code: str,
    *,
    as_of: str,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    db: Path | None = None,
) -> dict:
    """Compare two latest source snapshots no later than an explicit requested date.

    An earlier source snapshot does *not* establish unchanged membership on a
    later requested date. Such output is indicative, never verified as-of.
    """
    if left_code not in INDEX_NAMES or right_code not in INDEX_NAMES or left_code == right_code:
        raise ValueError("Choose two different supported constituent indexes")
    if len(query) > 80 or any(ord(ch) < 32 for ch in query):
        raise ValueError("Invalid constituent search")
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise ValueError("Invalid overlap pagination")
    when = _as_of(as_of)
    conn = _read_connection(db if db is not None else database_path())
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn else 0
        records = [
            _latest(conn, code, when) if conn is not None and version >= 5 else None
            for code in (left_code, right_code)
        ]
        sources = [
            {
                "code": code,
                "name": INDEX_NAMES[code],
                "requested_as_of": when,
                "snapshot_as_of": record["as_of"] if record else None,
                "source": record["source"] if record else None,
                "source_kind": record["source_kind"] if record else None,
                "source_as_of": record["source_as_of"] if record else None,
                "validation_status": record["validation_status"] if record else None,
                "availability_status": record["availability_status"] if record else "not_imported",
                "confidence": record["confidence"] if record else None,
                "expected_count": record["expected_count"] if record else None,
                "member_count": record["total"] if record else None,
            }
            for code, record in zip((left_code, right_code), records, strict=True)
        ]
        response = {
            "left_code": left_code,
            "right_code": right_code,
            "requested_as_of": when,
            "sources": sources,
            "status": "unavailable",
            "common_members": None,
            "union_members": None,
            "jaccard_ratio": None,
            "limit": limit,
            "offset": offset,
            "query": query,
            "filtered_total": None,
            "items": [],
            "snapshot_only": True,
            "no_lookahead": True,
            "read_only": True,
        }
        if any(record is None for record in records):
            # Unknown != zero. Never imply absence from an unavailable snapshot.
            return response
        left, right = records
        assert left is not None and right is not None
        if any(record["source_kind"] == "fund_derived_proxy" for record in records):
            status = "proxy_only"
        elif any(record["validation_status"] == "incomplete" for record in records):
            status = "incomplete"
        elif not all(_authoritative_snapshot(record) for record in records):
            status = "unverified"
        elif any(record["as_of"] != when or record["source_as_of"] != when for record in records):
            status = "earlier_snapshot"
        else:
            status = "verified_snapshot"

        base = (
            "FROM index_memberships a JOIN index_memberships b "
            "ON a.symbol=b.symbol "
            "LEFT JOIN symbol_catalog c ON c.symbol=a.symbol "
            "WHERE a.index_code=? AND a.as_of=? AND b.index_code=? AND b.as_of=?"
        )
        params = (left_code, left["as_of"], right_code, right["as_of"])
        common = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        union = left["total"] + right["total"] - common
        response.update(
            {
                "status": status,
                "common_members": common,
                "union_members": union,
                "jaccard_ratio": str(Decimal(common) / Decimal(union)) if union else None,
            }
        )
        filter_sql = ""
        filter_params: tuple[str, ...] = ()
        if query.strip():
            pattern = _matching_pattern(query.strip())
            filter_sql = " AND (a.symbol LIKE ? ESCAPE '!' OR c.name LIKE ? ESCAPE '!')"
            filter_params = (pattern, pattern)
        response["filtered_total"] = conn.execute(
            "SELECT COUNT(*) " + base + filter_sql, (*params, *filter_params)
        ).fetchone()[0]
        response["items"] = [
            dict(row)
            for row in conn.execute(
                "SELECT a.symbol, COALESCE(c.name,a.symbol) AS name, "
                "COALESCE(c.asset_type,'Unknown') AS asset_type "
                + base
                + filter_sql
                + " ORDER BY a.symbol LIMIT ? OFFSET ?",
                (*params, *filter_params, limit, offset),
            )
        ]
        return response
    except sqlite3.Error as exc:
        raise StorageError("Could not read local dated constituent overlap") from exc
    finally:
        if conn is not None:
            conn.close()
