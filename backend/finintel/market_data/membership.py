"""Import dated, explicitly sourced index constituents into local SQLite.

The supplied CSV must be independently licensed/authorized for private use.
No constituents are guessed from the ticker list or market prices.
"""

import argparse
import csv
from datetime import date
from pathlib import Path

from finintel.market_data.provider import SYMBOL_PATTERN
from finintel.storage.db import _read_connection, _write_connection, database_path


def load_membership(
    index_code: str, as_of: str, source: str, symbols: list[str], *, db: Path | None = None
) -> int:
    if index_code not in ("SP500", "DJIA"):
        raise ValueError("Unknown index")
    date.fromisoformat(as_of)
    if not source or len(source) > 200 or any(ord(c) < 32 for c in source):
        raise ValueError("Provide a safe source reference")
    tickers = {symbol.strip().upper() for symbol in symbols}
    if not tickers or any(not SYMBOL_PATTERN.fullmatch(item) for item in tickers):
        raise ValueError("Membership has unsupported symbols")
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            conn.execute(
                "DELETE FROM index_memberships WHERE index_code=? AND as_of=?", (index_code, as_of)
            )
            conn.executemany(
                "INSERT INTO index_memberships(index_code,symbol,as_of,source) VALUES (?,?,?,?)",
                ((index_code, symbol, as_of, source) for symbol in sorted(tickers)),
            )
        return len(tickers)
    finally:
        conn.close()


def memberships(symbol: str, *, db: Path | None = None) -> list[dict[str, str]]:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return []
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            return []
        rows = conn.execute(
            "SELECT index_code,symbol,as_of,source FROM index_memberships "
            "WHERE symbol=? ORDER BY as_of DESC, index_code",
            (symbol.upper(),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", choices=("SP500", "DJIA"), required=True)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD snapshot date")
    parser.add_argument("--source", required=True, help="Source identifier or licensed dataset")
    parser.add_argument("--csv", type=Path, required=True, help="CSV with symbol column")
    args = parser.parse_args()
    if not args.csv.is_file() or args.csv.stat().st_size > 1_000_000:
        parser.error("CSV must exist and be at most 1 MB")
    with args.csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            parser.error("CSV requires a symbol column")
        symbols = [row["symbol"] for row in reader]
    print(load_membership(args.index, args.as_of, args.source, symbols))


if __name__ == "__main__":
    main()
