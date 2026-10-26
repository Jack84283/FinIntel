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
from finintel.storage.db import _read_connection, _write_connection, database_path

INDEX_NAMES = {
    "SP500": "S&P 500",
    "DJIA": "Dow Jones Industrial Average",
    "NDX": "Nasdaq-100",
    "RUT": "Russell 2000",
}
MAX_SYMBOLS = 5000


def _as_of(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    parsed = date.fromisoformat(value)
    if parsed > datetime.now(UTC).date():
        raise ValueError("Index snapshot date cannot be in the future")
    return parsed.isoformat()


def load_membership(
    index_code: str,
    as_of: str,
    source: str,
    symbols: list[str],
    *,
    db: Path | None = None,
    expected_count: int | None = None,
) -> int:
    """Atomically replace exactly one index/date snapshot; retain all other dates."""
    if index_code not in INDEX_NAMES:
        raise ValueError("Unknown index")
    effective_date = _as_of(as_of)
    if not source or len(source) > 200 or any(ord(c) < 32 for c in source):
        raise ValueError("Provide a safe, independently verifiable source reference")
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
    conn = _write_connection(db if db is not None else database_path())
    try:
        # A recognized ETF/mutual fund/index is not an equity constituent.
        rows = conn.execute(
            "SELECT symbol, asset_type FROM symbol_catalog "
            "WHERE symbol IN (" + ",".join("?" for _ in tickers) + ")",
            tickers,
        ).fetchall()
        if any(row["asset_type"] != "Stock" for row in rows):
            raise ValueError("Index constituent list includes a known non-stock instrument")
        with conn:
            conn.execute(
                "DELETE FROM index_memberships WHERE index_code=? AND as_of=?",
                (index_code, effective_date),
            )
            conn.executemany(
                "INSERT INTO index_memberships(index_code,symbol,as_of,source) "
                "VALUES (?,?,?,?)",
                ((index_code, symbol, effective_date, source) for symbol in sorted(tickers)),
            )
        return len(tickers)
    finally:
        conn.close()


def _latest(conn: sqlite3.Connection, code: str, when: str) -> tuple[str, str, int] | None:
    row = conn.execute(
        "SELECT as_of, MAX(source) AS source, COUNT(*) AS total "
        "FROM index_memberships WHERE index_code=? AND as_of=("
        "SELECT MAX(as_of) FROM index_memberships WHERE index_code=? AND as_of<=?) "
        "GROUP BY as_of",
        (code, code, when),
    ).fetchone()
    return (row["as_of"], row["source"], row["total"]) if row else None


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
                "snapshot_as_of": record[0] if record else None,
                "source": record[1] if record else None,
                "members": record[2] if record else None,
                "status": "available" if record else "not_imported",
            }
            for code, name in INDEX_NAMES.items()
            for record in [
                _latest(conn, code, when)
                if conn is not None and version >= 5
                else None
            ]
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
            "snapshot_as_of": record[0] if record else None,
            "source": record[1] if record else None,
            "snapshot_members": record[2] if record else None,
            "total": 0,
            "limit": limit,
            "offset": offset,
            "items": [],
        }
        if not record:
            return result
        filter_sql = ""
        args = [code, record[0]]
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
                "COALESCE(c.asset_type, 'Unknown') AS asset_type " + base
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
            {**item, "member_indexes": sorted(other.get(item["symbol"], {code}))}
            for item in items
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
                        (entry["code"], entry["snapshot_as_of"]) in recorded
                        if entry["snapshot_as_of"] is not None
                        else None
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
            args.index, args.as_of, args.source, symbols, expected_count=args.expected_count
        )
    )


if __name__ == "__main__":
    main()
