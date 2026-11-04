"""Read-only history of *actually imported* dated index-membership snapshots.

Every row is a stored snapshot, not a filled daily series. Differences compare
only adjacent stored snapshots within the requested history window.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from finintel.market_data.membership import INDEX_NAMES, _as_of, _authoritative_snapshot, _latest
from finintel.storage.db import StorageError, _read_connection, database_path


def _differences(conn: sqlite3.Connection, code: str, before: str, after: str) -> tuple[int, int]:
    """Count set differences in SQLite; never load all symbols into application memory."""
    added = conn.execute(
        "SELECT COUNT(*) FROM index_memberships now "
        "WHERE now.index_code=? AND now.as_of=? AND NOT EXISTS ("
        "SELECT 1 FROM index_memberships previous WHERE previous.index_code=? "
        "AND previous.as_of=? AND previous.symbol=now.symbol)",
        (code, after, code, before),
    ).fetchone()[0]
    removed = conn.execute(
        "SELECT COUNT(*) FROM index_memberships previous "
        "WHERE previous.index_code=? AND previous.as_of=? AND NOT EXISTS ("
        "SELECT 1 FROM index_memberships now WHERE now.index_code=? "
        "AND now.as_of=? AND now.symbol=previous.symbol)",
        (code, before, code, after),
    ).fetchone()[0]
    return added, removed


def _evidence_status(record: dict) -> str:
    if record["source_kind"] == "fund_derived_proxy":
        return "proxy_only"
    if record["validation_status"] == "incomplete":
        return "incomplete"
    if not _authoritative_snapshot(record):
        return "unverified"
    if record["source_as_of"] != record["as_of"]:
        return "source_date_mismatch"
    return "verified_snapshot"


def constituent_snapshot_trend(
    code: str,
    *,
    from_as_of: str,
    to_as_of: str,
    limit: int = 25,
    offset: int = 0,
    db: Path | None = None,
) -> dict:
    """Return bounded historical snapshots; do not project constituents across gaps.

    The first row inside a requested range has *unknown* preceding change, even
    if older snapshots exist outside that range. Page offsets retain the prior
    in-range snapshot so changes are consistent across page boundaries.
    """
    if code not in INDEX_NAMES:
        raise ValueError("Unknown constituent index")
    start, end = _as_of(from_as_of), _as_of(to_as_of)
    if start > end:
        raise ValueError("Snapshot trend dates are out of order")
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise ValueError("Invalid snapshot trend pagination")

    conn = _read_connection(db if db is not None else database_path())
    response = {
        "code": code,
        "name": INDEX_NAMES[code],
        "from_as_of": start,
        "to_as_of": end,
        "total_snapshots": 0,
        "limit": limit,
        "offset": offset,
        "snapshots": [],
        "snapshot_only": True,
        "no_interpolation": True,
        "no_lookahead": True,
        "read_only": True,
    }
    if conn is None:
        return response
    try:
        # One consistent SQLite read transaction while other jobs might import.
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            return response
        where = "WHERE index_code=? AND as_of>=? AND as_of<=?"
        args = (code, start, end)
        response["total_snapshots"] = conn.execute(
            "SELECT COUNT(DISTINCT as_of) FROM index_memberships " + where, args
        ).fetchone()[0]
        # Include one preceding in-range date to compute the first displayed
        # delta on any page after the first; never compare to a future snapshot.
        from_offset = max(offset - 1, 0)
        size = limit + (1 if offset else 0)
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT as_of FROM index_memberships "
                + where
                + " ORDER BY as_of LIMIT ? OFFSET ?",
                (*args, size, from_offset),
            )
        ]
        previous: dict | None = None
        for position, day in enumerate(dates):
            record = _latest(conn, code, day)
            if record is None or record["as_of"] != day:
                raise StorageError("Dated membership snapshot became unavailable")
            status = _evidence_status(record)
            added: int | None = None
            removed: int | None = None
            change_status = "first_observed"
            if previous is not None:
                added, removed = _differences(conn, code, previous["as_of"], day)
                if previous["total"] + added - removed != record["total"]:
                    raise StorageError("Inconsistent membership snapshot differences")
                change_status = (
                    "verified_pair"
                    if _evidence_status(previous) == "verified_snapshot"
                    and status == "verified_snapshot"
                    else "indicative_pair"
                )
            item = {
                "snapshot_as_of": day,
                "source": record["source"],
                "source_as_of": record["source_as_of"],
                "source_kind": record["source_kind"],
                "validation_status": record["validation_status"],
                "availability_status": record["availability_status"],
                "confidence": record["confidence"],
                "expected_count": record["expected_count"],
                "member_count": record["total"],
                "evidence_status": status,
                "previous_snapshot_as_of": previous["as_of"] if previous else None,
                "previous_evidence_status": _evidence_status(previous) if previous else None,
                "added_members": added,
                "removed_members": removed,
                "change_status": change_status,
            }
            if not (offset and position == 0):
                response["snapshots"].append(item)
            previous = record
        return response
    except sqlite3.Error as exc:
        raise StorageError("Could not read local membership snapshot trends") from exc
    finally:
        conn.close()
