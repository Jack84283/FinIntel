"""Read-only coverage, comparison and query performance diagnostics.

Weekday gaps are candidates, not exchange-calendar-confirmed missing sessions.
Corporate action discontinuities are screening signals, not proof of splits.
"""

import argparse
import json
import sqlite3
import time
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finintel.storage.db import database_path


def _weekdays_between(start: date, end: date) -> int:
    """Count weekdays strictly between two trading dates, without a holiday calendar."""
    if end <= start:
        return 0
    days = (end - start).days - 1
    weeks, rem = divmod(days, 7)
    first = start + timedelta(days=1)
    return weeks * 5 + sum((first + timedelta(days=i)).weekday() < 5 for i in range(rem))


def diagnostics(db: Path, *, examples_limit: int = 30) -> dict:
    if not db.is_file():
        raise FileNotFoundError(db)
    start = time.monotonic()
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        series = conn.execute(
            "SELECT id, symbol, provider, adjustment FROM series ORDER BY id"
        ).fetchall()
        by_symbol = {}
        for item in series:
            by_symbol.setdefault(item["symbol"], []).append(dict(item))
        gaps = Counter()
        gap_examples = []
        corporate_candidates = []
        totals = {}
        comparisons = []
        compared = 0
        timings = {}
        for item in series:
            sid = item["id"]
            begin = time.monotonic()
            # Primary-key order supports bounded per-series reads.
            cursor = conn.execute(
                "SELECT session_date, close FROM daily_prices "
                "WHERE series_id=? ORDER BY session_date", (sid,)
            )
            prev_day = None
            prev_close = None
            count = 0
            first = None
            last = None
            for row in cursor:
                count += 1
                try:
                    day = date.fromisoformat(row["session_date"])
                    close = Decimal(row["close"])
                    if not close.is_finite() or close <= 0:
                        continue
                except (ValueError, TypeError, InvalidOperation):
                    continue
                if first is None:
                    first = day.isoformat()
                last = day.isoformat()
                if prev_day is not None:
                    missing = _weekdays_between(prev_day, day)
                    if missing:
                        gaps["weekday_gap_candidates"] += missing
                        gaps["series_with_gaps_" + str(sid)] += 1
                        if len(gap_examples) < examples_limit:
                            gap_examples.append({
                                "symbol": item["symbol"], "provider": item["provider"],
                                "previous": prev_day.isoformat(), "next": day.isoformat(),
                                "weekday_candidates": missing,
                            })
                    if prev_close is not None:
                        ratio = close / prev_close
                        if (ratio >= 2 or ratio <= Decimal("0.5")) and len(
                            corporate_candidates
                        ) < examples_limit:
                            corporate_candidates.append({
                                "symbol": item["symbol"], "provider": item["provider"],
                                "date": day.isoformat(), "close_ratio": str(round(ratio, 4)),
                                "adjustment": item["adjustment"],
                            })
                prev_day, prev_close = day, close
            totals[sid] = {"rows": count, "first": first, "last": last}
            timings[item["symbol"] + "/" + item["provider"]] = round(
                time.monotonic() - begin, 4
            )

        # Compare only exact overlapping sessions of two providers for the same symbol.
        # Never combine adjusted and raw series.
        for symbol, variants in by_symbol.items():
            if len(variants) < 2:
                continue
            for i, left in enumerate(variants):
                for right in variants[i + 1:]:
                    if left["provider"] == right["provider"] or (
                        left["adjustment"] != right["adjustment"]
                    ):
                        continue
                    begin = time.monotonic()
                    rows = conn.execute(
                        "SELECT a.session_date, a.close AS a_close, b.close AS b_close "
                        "FROM daily_prices a JOIN daily_prices b "
                        "ON b.session_date=a.session_date AND b.series_id=? "
                        "WHERE a.series_id=? ORDER BY a.session_date",
                        (right["id"], left["id"]),
                    )
                    overlap, mismatches = 0, 0
                    for row in rows:
                        overlap += 1
                        try:
                            a, b = Decimal(row["a_close"]), Decimal(row["b_close"])
                            if a > 0 and b > 0 and abs(a - b) / a > Decimal("0.01"):
                                mismatches += 1
                        except (InvalidOperation, TypeError):
                            mismatches += 1
                    compared += 1
                    if len(comparisons) < examples_limit:
                        comparisons.append({
                            "symbol": symbol, "providers": [left["provider"], right["provider"]],
                            "overlap_sessions": overlap, "close_divergence_over_1pct": mismatches,
                            "elapsed_seconds": round(time.monotonic() - begin, 4),
                        })

        slowest = sorted(timings.items(), key=lambda pair: pair[1], reverse=True)[:examples_limit]
        return {
            "database": str(db), "series_count": len(series),
            "series_without_prices": sum(x["rows"] == 0 for x in totals.values()),
            "coverage_examples": [
                {"symbol": item["symbol"], "provider": item["provider"], **totals[item["id"]]}
                for item in series[:examples_limit]
            ],
            "weekday_gap_candidates": gaps["weekday_gap_candidates"],
            "series_with_weekday_gap_candidates": sum(
                key.startswith("series_with_gaps_") for key in gaps
            ),
            "gap_examples": gap_examples,
            "corporate_action_jump_candidates": corporate_candidates,
            "cross_provider_pairs_compared": compared,
            "cross_provider_comparisons": comparisons,
            "slowest_series_queries_seconds": [
                {"series": name, "seconds": duration} for name, duration in slowest
            ],
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "limitations": [
                "Weekday gaps include exchange holidays and must not be treated as confirmed missing data.",
                "Coverage is observed first/last date, not proof of full 10-year history.",
                "Large price jumps may be splits or actual market moves; dividends are not verified.",
                "Cross-provider comparisons require matching adjustment labels and overlapping dates.",
                "Timings reflect this workstation and current SQLite cache, not fixed benchmarks.",
            ],
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only coverage and provider diagnostics")
    parser.add_argument("--db", type=Path, default=database_path())
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = diagnostics(args.db)
    output = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
