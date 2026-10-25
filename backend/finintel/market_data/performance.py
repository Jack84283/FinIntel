"""Low-cost, read-only SQLite performance probes, independent of full price audit."""

import sqlite3
import time
from pathlib import Path


def benchmark(db: Path, *, samples: int = 12) -> dict:
    if not db.is_file():
        raise FileNotFoundError(db)
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        begin = time.monotonic()
        instrument_rows = conn.execute(
            "SELECT id, symbol, provider FROM series ORDER BY symbol, provider"
        ).fetchall()
        directory_seconds = round(time.monotonic() - begin, 4)
        # Evenly spaced representative series; never scan the full prices table.
        step = max(1, len(instrument_rows) // max(1, samples))
        chosen = instrument_rows[::step][:samples]
        sample_timings = []
        for item in chosen:
            start = time.monotonic()
            bars = conn.execute(
                "SELECT session_date, open, high, low, close, volume "
                "FROM daily_prices WHERE series_id=? ORDER BY session_date DESC LIMIT 250",
                (item["id"],),
            ).fetchall()
            sample_timings.append(
                {
                    "symbol": item["symbol"],
                    "provider": item["provider"],
                    "rows": len(bars),
                    "seconds": round(time.monotonic() - start, 4),
                }
            )
        plan = [
            row["detail"]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT session_date, close FROM daily_prices "
                "WHERE series_id=? ORDER BY session_date DESC LIMIT 250",
                (0,),
            )
        ]
        # Conservative cold/warm-workstation targets; record actual values for review.
        # A failing target is a diagnostic signal, not proof of a database defect.
        slowest = max((item["seconds"] for item in sample_timings), default=0.0)
        acceptance = {
            "directory_under_2_seconds": directory_seconds <= 2.0,
            "sample_price_query_under_250ms": slowest <= 0.25,
            "uses_price_index": any("INDEX" in step or "PRIMARY KEY" in step for step in plan),
        }
        return {
            "acceptance": acceptance,
            "all_acceptance_targets_met": all(acceptance.values()),
            "thresholds": {
                "directory_seconds": 2.0,
                "sample_price_query_seconds": 0.25,
            },
            "series_count": len(instrument_rows),
            "directory_seconds": directory_seconds,
            "sample_count": len(sample_timings),
            "sample_queries": sample_timings,
            "slowest_series_queries_seconds": sorted(
                sample_timings, key=lambda item: item["seconds"], reverse=True
            ),
            "performance_query_plan": plan,
            "scope": "Read-only indexed directory and 250-row sample lookups; not a full-table benchmark.",
        }
    finally:
        conn.close()
