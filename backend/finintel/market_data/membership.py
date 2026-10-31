"""Dated, source-backed index membership snapshots and read-only discovery.

Do not infer constituents from ETF holdings, ticker price history, or today’s
listing directory. A snapshot is a point-in-time claim from its named source.
"""

import argparse
import csv
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from finintel.market_data.provider import SYMBOL_PATTERN
from finintel.storage.db import (
    StorageError,
    _read_connection,
    _write_connection,
    database_path,
    serialized_storage_write,
)

INDEX_NAMES = {
    "SP500": "S&P 500",
    "DJIA": "Dow Jones Industrial Average",
    "NDX": "Nasdaq-100",
    "RUT": "Russell 2000",
}
MAX_SYMBOLS = 5000
SOURCE_KINDS = {
    "official_licensed",
    "verified_public",
    "fund_derived_proxy",
    "unknown",
}
CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


def _as_of(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    parsed = date.fromisoformat(value)
    if parsed > datetime.now(UTC).date():
        raise ValueError("Index snapshot date cannot be in the future")
    return parsed.isoformat()


@serialized_storage_write
def load_membership(
    index_code: str,
    as_of: str,
    source: str,
    symbols: list[str],
    *,
    db: Path | None = None,
    expected_count: int | None = None,
    source_expected_count: int | None = None,
    source_kind: str = "unknown",
    confidence: str | None = None,
    names: dict[str, str] | None = None,
) -> int:
    """Atomically replace exactly one index/date snapshot; retain all other dates."""
    if index_code not in INDEX_NAMES:
        raise ValueError("Unknown index")
    effective_date = _as_of(as_of)
    if not source or len(source) > 200 or any(ord(c) < 32 for c in source):
        raise ValueError("Provide a safe, independently verifiable source reference")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("Invalid membership source kind")
    if confidence is not None and confidence not in CONFIDENCE_LEVELS:
        raise ValueError("Invalid membership confidence")
    tickers = [symbol.strip().upper() for symbol in symbols]
    if (
        not tickers
        or len(tickers) > MAX_SYMBOLS
        or len(set(tickers)) != len(tickers)
        or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in tickers)
    ):
        raise ValueError("Membership must contain unique valid stock tickers")
    if expected_count is not None and (expected_count < 1 or expected_count != len(tickers)):
        raise ValueError("Snapshot does not match the source's expected constituent count")
    if source_expected_count is not None and (
        source_expected_count < 1 or source_expected_count < len(tickers)
    ):
        raise ValueError("Snapshot exceeds the source's declared constituent count")
    if names is not None and (
        set(names) != set(tickers)
        or any(
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 200
            or any(ord(ch) < 32 for ch in name)
            for name in names.values()
        )
    ):
        raise ValueError("Equibles company names are incomplete or invalid")
    conn = _write_connection(db if db is not None else database_path())
    try:
        # A recognized ETF/mutual fund/index is not an equity constituent.
        for start in range(0, len(tickers), 400):
            part = tickers[start : start + 400]
            rows = conn.execute(
                "SELECT symbol, asset_type FROM symbol_catalog "
                "WHERE symbol IN (" + ",".join("?" for _ in part) + ")",
                part,
            ).fetchall()
            if any(row["asset_type"] != "Stock" for row in rows):
                raise ValueError("Index constituent list includes a known non-stock instrument")
        with conn:
            imported_at = datetime.now(UTC).isoformat(timespec="microseconds")
            if names is not None:
                conn.executemany(
                    "INSERT OR IGNORE INTO symbol_catalog "
                    "(symbol, name, asset_type, exchange, source, updated_at) "
                    "VALUES (?, ?, 'Stock', '', 'equibles_proxy', ?)",
                    ((symbol, names[symbol], imported_at) for symbol in tickers),
                )
            conn.execute(
                "DELETE FROM index_memberships WHERE index_code=? AND as_of=?",
                (index_code, effective_date),
            )
            conn.executemany(
                "INSERT INTO index_memberships(index_code,symbol,as_of,source) VALUES (?,?,?,?)",
                ((index_code, symbol, effective_date, source) for symbol in sorted(tickers)),
            )
            expected_members = (
                source_expected_count
                if source_expected_count is not None
                else expected_count
                if expected_count is not None
                else None
            )
            validation_status = (
                "unknown"
                if expected_members is None
                else "complete"
                if expected_members == len(tickers)
                else "incomplete"
            )
            if source_kind == "fund_derived_proxy":
                availability_status = "proxy_only"
            elif validation_status == "incomplete":
                availability_status = "incomplete"
            elif validation_status == "unknown" or source_kind == "unknown":
                availability_status = "unverified"
            else:
                availability_status = "available"
            resolved_confidence = (
                confidence
                or {
                    "official_licensed": "high",
                    "verified_public": "medium",
                    "fund_derived_proxy": "low",
                    "unknown": "unknown",
                }[source_kind]
            )
            conn.execute(
                "INSERT INTO index_membership_snapshots("
                "index_code,as_of,source,source_kind,source_as_of,expected_count,member_count,"
                "validation_status,availability_status,confidence,imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(index_code,as_of) DO UPDATE SET source=excluded.source,"
                "source_kind=excluded.source_kind,source_as_of=excluded.source_as_of,"
                "expected_count=excluded.expected_count,member_count=excluded.member_count,"
                "validation_status=excluded.validation_status,"
                "availability_status=excluded.availability_status,confidence=excluded.confidence,"
                "imported_at=excluded.imported_at",
                (
                    index_code,
                    effective_date,
                    source,
                    source_kind,
                    effective_date,
                    expected_members,
                    len(tickers),
                    validation_status,
                    availability_status,
                    resolved_confidence,
                    imported_at,
                ),
            )
        return len(tickers)
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            "Index import violates a local database constraint; check symbols and catalog types"
        ) from exc
    except sqlite3.Error as exc:
        raise StorageError(
            "Index import failed while writing the local database; no complete snapshot was saved"
        ) from exc
    finally:
        conn.close()


def _legacy_metadata(source: str, member_count: int, as_of: str) -> dict[str, object]:
    proxy = source.startswith("Equibles / ETF holdings proxy;")
    incomplete = "; incomplete:" in source
    return {
        "source_kind": "fund_derived_proxy" if proxy else "unknown",
        "source_as_of": as_of,
        "expected_count": member_count,
        "member_count": member_count,
        "validation_status": "incomplete" if incomplete else "unknown",
        "availability_status": "proxy_only" if proxy else "unverified",
        "confidence": "low" if proxy else "unknown",
    }


def _authoritative_snapshot(record: dict[str, object]) -> bool:
    return (
        record.get("source_kind") in {"official_licensed", "verified_public"}
        and record.get("validation_status") == "complete"
        and record.get("availability_status") == "available"
    )


def _latest(conn: sqlite3.Connection, code: str, when: str) -> dict[str, object] | None:
    metadata_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_membership_snapshots'"
    ).fetchone()
    if metadata_table:
        row = conn.execute(
            "SELECT m.as_of,MAX(m.source) AS source,COUNT(*) AS total,"
            "s.source_kind,s.source_as_of,s.expected_count,s.member_count,"
            "s.validation_status,s.availability_status,s.confidence "
            "FROM index_memberships m LEFT JOIN index_membership_snapshots s "
            "ON s.index_code=m.index_code AND s.as_of=m.as_of "
            "WHERE m.index_code=? AND m.as_of=("
            "SELECT MAX(as_of) FROM index_memberships WHERE index_code=? AND as_of<=?) "
            "GROUP BY m.as_of,s.source_kind,s.source_as_of,s.expected_count,s.member_count,"
            "s.validation_status,s.availability_status,s.confidence",
            (code, code, when),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        if result["source_kind"] is None:
            result.update(_legacy_metadata(result["source"], result["total"], result["as_of"]))
        else:
            result["member_count"] = result["member_count"] or result["total"]
        return result
    row = conn.execute(
        "SELECT as_of,MAX(source) AS source,COUNT(*) AS total "
        "FROM index_memberships WHERE index_code=? AND as_of=("
        "SELECT MAX(as_of) FROM index_memberships WHERE index_code=? AND as_of<=?) "
        "GROUP BY as_of",
        (code, code, when),
    ).fetchone()
    if not row:
        return None
    return {
        "as_of": row["as_of"],
        "source": row["source"],
        "total": row["total"],
        **_legacy_metadata(row["source"], row["total"], row["as_of"]),
    }


def list_indexes(*, as_of: str | None = None, db: Path | None = None) -> list[dict]:
    """Show explicitly unknown indexes; never imply zero members when unsourced."""
    when = _as_of(as_of)
    conn = _read_connection(db if db is not None else database_path())
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn else 0
        return [
            {
                "code": code,
                "name": name,
                "requested_as_of": when,
                "snapshot_as_of": record["as_of"] if record else None,
                "source": record["source"] if record else None,
                "members": record["member_count"] if record else None,
                "source_kind": record["source_kind"] if record else None,
                "source_as_of": record["source_as_of"] if record else None,
                "expected_count": record["expected_count"] if record else None,
                "validation_status": record["validation_status"] if record else None,
                "availability_status": record["availability_status"] if record else "not_imported",
                "confidence": record["confidence"] if record else None,
                "status": "available" if record else "not_imported",
            }
            for code, name in INDEX_NAMES.items()
            for record in [_latest(conn, code, when) if conn is not None and version >= 5 else None]
        ]
    finally:
        if conn is not None:
            conn.close()


def _matching_pattern(query: str) -> str:
    return "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def index_constituents(
    code: str,
    *,
    as_of: str | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    db: Path | None = None,
) -> dict:
    if code not in INDEX_NAMES:
        raise ValueError("Unknown index")
    if len(query) > 80 or not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid index constituent search")
    when = _as_of(as_of)
    conn = _read_connection(db if db is not None else database_path())
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn else 0
        record = _latest(conn, code, when) if conn is not None and version >= 5 else None
        result = {
            "code": code,
            "name": INDEX_NAMES[code],
            "requested_as_of": when,
            "snapshot_as_of": record["as_of"] if record else None,
            "source": record["source"] if record else None,
            "snapshot_members": record["member_count"] if record else None,
            "source_kind": record["source_kind"] if record else None,
            "source_as_of": record["source_as_of"] if record else None,
            "expected_count": record["expected_count"] if record else None,
            "validation_status": record["validation_status"] if record else None,
            "availability_status": record["availability_status"] if record else "not_imported",
            "confidence": record["confidence"] if record else None,
            "total": 0,
            "limit": limit,
            "offset": offset,
            "items": [],
        }
        if not record:
            return result
        filter_sql = ""
        args = [code, record["as_of"]]
        if query.strip():
            pattern = _matching_pattern(query.strip())
            filter_sql = " AND (m.symbol LIKE ? ESCAPE '!' OR c.name LIKE ? ESCAPE '!')"
            args.extend([pattern, pattern])
        base = (
            "FROM index_memberships m "
            "LEFT JOIN symbol_catalog c ON c.symbol=m.symbol "
            "WHERE m.index_code=? AND m.as_of=?" + filter_sql
        )
        result["total"] = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
        items = [
            dict(row)
            for row in conn.execute(
                "SELECT m.symbol, COALESCE(c.name, m.symbol) AS name, "
                "COALESCE(c.asset_type, 'Unknown') AS asset_type "
                + base
                + " ORDER BY m.symbol LIMIT ? OFFSET ?",
                (*args, limit, offset),
            )
        ]
        # For every displayed stock, show membership in *other* indexes
        # according to each index's latest snapshot no later than the date.
        latest = {
            entry["code"]: entry["snapshot_as_of"] for entry in list_indexes(as_of=when, db=db)
        }
        other = {}
        if items:
            symbols = [item["symbol"] for item in items]
            for row in conn.execute(
                "SELECT index_code, symbol, as_of FROM index_memberships WHERE "
                "symbol IN (" + ",".join("?" for _ in symbols) + ") AND as_of<=?",
                (*symbols, when),
            ):
                if latest.get(row["index_code"]) == row["as_of"]:
                    other.setdefault(row["symbol"], set()).add(row["index_code"])
        result["items"] = [
            {**item, "member_indexes": sorted(other.get(item["symbol"], {code}))} for item in items
        ]
        return result
    finally:
        if conn is not None:
            conn.close()


def membership_changes(
    code: str,
    *,
    from_as_of: str,
    to_as_of: str,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    db: Path | None = None,
) -> dict:
    """Compare two effective snapshots without treating missing data as no change."""
    if code not in INDEX_NAMES:
        raise ValueError("Unknown index")
    if len(query) > 80 or not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid index membership change search")
    start = _as_of(from_as_of)
    end = _as_of(to_as_of)
    if start > end:
        raise ValueError("Change range must end on or after its start date")
    conn = _read_connection(db if db is not None else database_path())
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn else 0
        before = _latest(conn, code, start) if conn is not None and version >= 5 else None
        after = _latest(conn, code, end) if conn is not None and version >= 5 else None
        result = {
            "code": code,
            "name": INDEX_NAMES[code],
            "from_requested_as_of": start,
            "to_requested_as_of": end,
            "from_snapshot_as_of": before["as_of"] if before else None,
            "to_snapshot_as_of": after["as_of"] if after else None,
            "from_source": before["source"] if before else None,
            "to_source": after["source"] if after else None,
            "status": (
                "unknown"
                if not before or not after
                else "available"
                if _authoritative_snapshot(before) and _authoritative_snapshot(after)
                else "uncertain"
            ),
            "total": 0,
            "limit": limit,
            "offset": offset,
            "items": [],
        }
        if not before or not after:
            return result
        before_rows = conn.execute(
            "SELECT symbol FROM index_memberships WHERE index_code=? AND as_of=?",
            (code, before["as_of"]),
        ).fetchall()
        after_rows = conn.execute(
            "SELECT symbol FROM index_memberships WHERE index_code=? AND as_of=?",
            (code, after["as_of"]),
        ).fetchall()
        before_symbols = {row["symbol"] for row in before_rows}
        after_symbols = {row["symbol"] for row in after_rows}
        changes = [(symbol, "added") for symbol in after_symbols - before_symbols]
        changes.extend((symbol, "removed") for symbol in before_symbols - after_symbols)
        changes.sort()
        names = (
            {
                row["symbol"]: (row["name"], row["asset_type"])
                for row in conn.execute(
                    "SELECT symbol,name,asset_type FROM symbol_catalog WHERE symbol IN ("
                    + ",".join("?" for _ in changes)
                    + ")",
                    [symbol for symbol, _ in changes],
                ).fetchall()
            }
            if changes
            else {}
        )
        if query.strip():
            lowered = query.strip().casefold()
            changes = [
                (symbol, change)
                for symbol, change in changes
                if lowered in symbol.casefold()
                or lowered in names.get(symbol, (symbol, "Unknown"))[0].casefold()
            ]
        result["total"] = len(changes)
        result["items"] = [
            {
                "symbol": symbol,
                "name": names.get(symbol, (symbol, "Unknown"))[0],
                "asset_type": names.get(symbol, (symbol, "Unknown"))[1],
                "change": change,
            }
            for symbol, change in changes[offset : offset + limit]
        ]
        return result
    finally:
        if conn is not None:
            conn.close()


def symbol_index_memberships(
    symbol: str, *, as_of: str | None = None, db: Path | None = None
) -> dict:
    ticker = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(ticker):
        raise ValueError("Invalid ticker symbol")
    when = _as_of(as_of)
    index_status = list_indexes(as_of=when, db=db)
    conn = _read_connection(db if db is not None else database_path())
    try:
        rows = (
            conn.execute(
                "SELECT index_code, as_of FROM index_memberships WHERE symbol=? AND as_of<=?",
                (ticker, when),
            ).fetchall()
            if conn is not None and conn.execute("PRAGMA user_version").fetchone()[0] >= 5
            else []
        )
        recorded = {(r["index_code"], r["as_of"]) for r in rows}
        return {
            "symbol": ticker,
            "requested_as_of": when,
            "indexes": [
                {
                    **entry,
                    "is_member": (
                        True
                        if (entry["code"], entry["snapshot_as_of"]) in recorded
                        else None
                        if not _authoritative_snapshot(entry)
                        else False
                    ),
                }
                for entry in index_status
            ],
        }
    finally:
        if conn is not None:
            conn.close()


def memberships(symbol: str, *, db: Path | None = None, as_of: str | None = None) -> list[dict]:
    """Compatibility helper: memberships as of latest available dated snapshots."""
    result = symbol_index_memberships(symbol, as_of=as_of, db=db)
    return [
        {
            "index_code": item["code"],
            "symbol": result["symbol"],
            "as_of": item["snapshot_as_of"],
            "source": item["source"],
        }
        for item in result["indexes"]
        if item["is_member"] is True
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import licensed, dated index constituent CSV")
    parser.add_argument("--index", choices=INDEX_NAMES, required=True)
    parser.add_argument("--as-of", required=True, help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--source", required=True, help="Independently verified source reference")
    parser.add_argument("--csv", type=Path, required=True, help="CSV with a symbol header")
    parser.add_argument("--expected-count", type=int, help="Source-reported security count")
    parser.add_argument("--source-kind", choices=sorted(SOURCE_KINDS), default="unknown")
    parser.add_argument("--confidence", choices=sorted(CONFIDENCE_LEVELS))
    args = parser.parse_args()
    if not args.csv.is_file() or args.csv.stat().st_size > 1_000_000:
        parser.error("CSV must exist and be at most 1 MB")
    with args.csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            parser.error("CSV requires a symbol column")
        symbols = []
        for row in reader:
            if not isinstance(row, dict) or row.get("symbol") is None or None in row:
                parser.error("Malformed constituent CSV row")
            symbols.append(row["symbol"])
            if len(symbols) > MAX_SYMBOLS:
                parser.error("CSV exceeds the maximum supported constituent count")
    print(
        load_membership(
            args.index,
            args.as_of,
            args.source,
            symbols,
            expected_count=args.expected_count,
            source_kind=args.source_kind,
            confidence=args.confidence,
        )
    )


if __name__ == "__main__":
    main()
