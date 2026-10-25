"""Read-only quality audit for FinIntel's local historical market database."""

import argparse
import json
import sqlite3
import time
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finintel.storage.db import database_path


def validate(db: Path, *, sample_limit: int = 20) -> dict:
    """Audit every price row once; no schema changes, writes, or temp tables."""
    if not db.is_file():
        raise FileNotFoundError(f"Database not found: {db}")
    started = time.monotonic()
    uri = db.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        series = conn.execute("SELECT id, symbol, provider FROM series ORDER BY id").fetchall()
        providers = Counter(row["provider"] for row in series)
        issues = Counter()
        examples = []
        total = 0
        coverage = {}
        previous = None
        prev_date = None
        first = None
        last = None
        count = 0

        # Streaming cursor keeps memory bounded even for tens of millions of rows.
        cursor = conn.execute(
            "SELECT series_id, session_date, open, high, low, close, volume "
            "FROM daily_prices ORDER BY series_id, session_date"
        )
        for row in cursor:
            sid = row["series_id"]
            if previous is not None and sid != previous:
                coverage[previous] = {"rows": count, "first": first, "last": last}
                count, first, last, prev_date = 0, None, None, None
            previous = sid
            day = row["session_date"]
            if first is None:
                first = day
            last = day
            count += 1
            total += 1
            try:
                day_date = date.fromisoformat(day)
                prices = [Decimal(str(row[key])) for key in ("open", "high", "low", "close")]
                o, h, l, c = prices
                if not all(price.is_finite() and price > 0 for price in prices):
                    raise ValueError("nonpositive_or_nonfinite_price")
                if h < max(o, l, c) or l > min(o, h, c):
                    raise ValueError("invalid_ohlc_range")
                if row["volume"] is None or row["volume"] < 0:
                    raise ValueError("invalid_volume")
                if prev_date is not None and day_date <= prev_date:
                    raise ValueError("nonincreasing_session")
                prev_date = day_date
            except (ValueError, TypeError, InvalidOperation) as exc:
                reason = str(exc) or "invalid_price_or_date"
                issues[reason] += 1
                if len(examples) < sample_limit:
                    examples.append({"series_id": sid, "date": day, "issue": reason})
        if previous is not None:
            coverage[previous] = {"rows": count, "first": first, "last": last}
        empty = [dict(row) for row in series if row["id"] not in coverage]
        return {
            "database": str(db), "schema_version": schema,
            "series_count": len(series), "price_rows": total,
            "provider_series": dict(sorted(providers.items())),
            "series_with_prices": len(coverage),
            "series_without_prices": len(empty),
            "empty_series_examples": empty[:sample_limit],
            "issues": dict(sorted(issues.items())), "issue_examples": examples,
            "integrity": "issues_found" if issues else "no_row_level_issues_found",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "notes": [
                "This checks stored OHLCV rows, not exchange-calendar completeness.",
                "A zero issue count does not establish provider accuracy or adjustment correctness.",
                "The database was opened read-only; no data was modified.",
            ],
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only FinIntel price data audit")
    parser.add_argument("--db", type=Path, default=database_path())
    parser.add_argument("--json", type=Path, help="Optional destination for JSON report")
    args = parser.parse_args()
    report = validate(args.db)
    output = json.dumps(report, indent=2)
    if args.json:
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    if report["issues"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
