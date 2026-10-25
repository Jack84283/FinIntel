"""Read-only coverage, comparison and query performance diagnostics.

Weekday gaps are candidates, not exchange-calendar-confirmed missing sessions.
Corporate action discontinuities are screening signals, not proof of splits.
"""

import argparse
import json
import sqlite3
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import exchange_calendars as xcals

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
        # A local liquid benchmark filters shared non-trading days without network access.
        reference = next(
            (
                item
                for item in series
                if item["symbol"] == "SPY" and item["provider"] == "alpaca_sip"
            ),
            None,
        )
        reference_days = []
        if reference is not None:
            reference_days = [
                row[0]
                for row in conn.execute(
                    "SELECT session_date FROM daily_prices WHERE series_id=? ORDER BY session_date",
                    (reference["id"],),
                )
            ]
        # Calendar sessions are authoritative for NYSE-listed equities and ETFs.
        nyse = xcals.get_calendar(
            "XNYS", start="2000-01-01", end=(date.today() + timedelta(days=365)).isoformat()
        )
        official_days = [session.date().isoformat() for session in nyse.sessions]
        official_gaps = Counter()
        official_examples = []
        gaps = Counter()
        reference_gaps = Counter()
        reference_gap_examples = []
        gap_examples = []
        corporate_candidates = []
        totals = {}
        comparisons = []
        compared = 0
        timings = {}
        provider_pair_divergences = 0
        jump_count = 0
        coverage_years = Counter()
        for item in series:
            sid = item["id"]
            begin = time.monotonic()
            # Primary-key order supports bounded per-series reads.
            cursor = conn.execute(
                "SELECT session_date, close FROM daily_prices "
                "WHERE series_id=? ORDER BY session_date",
                (sid,),
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
                    if reference_days and prev_day < day:
                        confirmed_by_reference = max(
                            0,
                            bisect_left(reference_days, day.isoformat())
                            - bisect_right(reference_days, prev_day.isoformat()),
                        )
                        if confirmed_by_reference > 0:
                            reference_gaps["sessions"] += confirmed_by_reference
                            reference_gaps["series_" + str(sid)] += 1
                            if len(reference_gap_examples) < examples_limit:
                                reference_gap_examples.append(
                                    {
                                        "symbol": item["symbol"],
                                        "provider": item["provider"],
                                        "previous": prev_day.isoformat(),
                                        "next": day.isoformat(),
                                        "reference_sessions_missing": confirmed_by_reference,
                                    }
                                )
                    official_count = max(
                        0,
                        bisect_left(official_days, day.isoformat())
                        - bisect_right(official_days, prev_day.isoformat()),
                    )
                    if official_count:
                        official_gaps["sessions"] += official_count
                        official_gaps["series_" + str(sid)] += 1
                        if len(official_examples) < examples_limit:
                            official_examples.append(
                                {
                                    "symbol": item["symbol"],
                                    "provider": item["provider"],
                                    "previous": prev_day.isoformat(),
                                    "next": day.isoformat(),
                                    "expected_xnys_sessions": official_count,
                                }
                            )
                    if missing:
                        gaps["weekday_gap_candidates"] += missing
                        gaps["series_with_gaps_" + str(sid)] += 1
                        if len(gap_examples) < examples_limit:
                            gap_examples.append(
                                {
                                    "symbol": item["symbol"],
                                    "provider": item["provider"],
                                    "previous": prev_day.isoformat(),
                                    "next": day.isoformat(),
                                    "weekday_candidates": missing,
                                }
                            )
                    if prev_close is not None:
                        ratio = close / prev_close
                        if ratio >= 2 or ratio <= Decimal("0.5"):
                            jump_count += 1
                        if (ratio >= 2 or ratio <= Decimal("0.5")) and len(
                            corporate_candidates
                        ) < examples_limit:
                            corporate_candidates.append(
                                {
                                    "symbol": item["symbol"],
                                    "provider": item["provider"],
                                    "date": day.isoformat(),
                                    "close_ratio": str(round(ratio, 4)),
                                    "adjustment": item["adjustment"],
                                }
                            )
                prev_day, prev_close = day, close
            totals[sid] = {"rows": count, "first": first, "last": last}
            if first and last:
                span = (date.fromisoformat(last) - date.fromisoformat(first)).days
                coverage_years[
                    "under_1_year"
                    if span < 365
                    else "under_5_years"
                    if span < 1826
                    else "under_9_years"
                    if span < 3287
                    else "9_plus_years"
                ] += 1
            timings[item["symbol"] + "/" + item["provider"]] = round(time.monotonic() - begin, 4)

        # Compare only exact overlapping sessions of two providers for the same symbol.
        # Never combine adjusted and raw series.
        for symbol, variants in by_symbol.items():
            if len(variants) < 2:
                continue
            for i, left in enumerate(variants):
                for right in variants[i + 1 :]:
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
                    provider_pair_divergences += mismatches
                    if len(comparisons) < examples_limit:
                        comparisons.append(
                            {
                                "symbol": symbol,
                                "providers": [left["provider"], right["provider"]],
                                "overlap_sessions": overlap,
                                "close_divergence_over_1pct": mismatches,
                                "elapsed_seconds": round(time.monotonic() - begin, 4),
                            }
                        )

        slowest = sorted(timings.items(), key=lambda pair: pair[1], reverse=True)[:examples_limit]
        return {
            "database": str(db),
            "series_count": len(series),
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
            "xnys_internal_gap_candidates": official_gaps["sessions"],
            "series_with_xnys_internal_gaps": sum(
                key.startswith("series_") for key in official_gaps
            ),
            "xnys_gap_examples": official_examples,
            "reference_symbol": reference["symbol"] if reference else None,
            "reference_provider": reference["provider"] if reference else None,
            "reference_session_gap_candidates": reference_gaps["sessions"],
            "series_with_reference_session_gaps": sum(
                key.startswith("series_") for key in reference_gaps
            ),
            "reference_gap_examples": reference_gap_examples,
            "observed_span_groups": dict(coverage_years),
            "series_with_less_than_9_years_observed": sum(
                value for key, value in coverage_years.items() if key != "9_plus_years"
            ),
            "corporate_action_jump_candidates": corporate_candidates,
            "corporate_action_jump_candidate_count": jump_count,
            "cross_provider_pairs_compared": compared,
            "cross_provider_close_divergences_over_1pct": provider_pair_divergences,
            "cross_provider_comparisons": comparisons,
            "performance_query_plan": [
                row["detail"]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT session_date, close FROM daily_prices "
                    "WHERE series_id=? ORDER BY session_date",
                    (0,),
                ).fetchall()
            ],
            "slowest_series_queries_seconds": [
                {"series": name, "seconds": duration} for name, duration in slowest
            ],
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "limitations": [
                "Weekday gaps include exchange holidays and must not be treated as confirmed missing data.",
                "Coverage groups use observed span; newer listings and delisted stocks may be shorter.",
                "Reference gaps use observed SPY sessions, not an authoritative exchange calendar.",
                "SPY and other assets can have different listing calendars, trading halts or provider outages.",
                "XNYS gaps use the official exchange calendar, but venue, listing and halt status still require verification.",
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
