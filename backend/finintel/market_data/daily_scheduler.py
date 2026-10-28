"""Local scheduled end-of-day refresh. Run as a separate process, never on API import.

A missed schedule is caught up at the next wake/start. Monitoring state is stored
outside the price database to avoid interfering with backup and restore.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import contextlib
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from finintel.market_data.acquisition import acquire_and_store
from finintel.market_data.index_history import INDEX_SERIES, acquire_index_closes
from finintel.market_data.provider import ProviderError, ProviderRateLimited
from finintel.storage.db import database_path

ET = ZoneInfo("America/New_York")
STATE = Path("data/daily-acquisition.json")


def now_utc() -> datetime:
    return datetime.now(UTC)


def due_date(now: datetime) -> str | None:
    """Trading-date eligibility, after 20:00 Eastern, with DST."""
    eastern = now.astimezone(ET)
    if eastern.weekday() >= 5 or (eastern.hour, eastern.minute) < (20, 0):
        return None
    return eastern.date().isoformat()


def status(path: Path = STATE) -> dict:
    if not path.exists():
        return {"status": "never_run", "last_completed_date": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(state, file, sort_keys=True, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def instruments(provider: str) -> list[str]:
    db = database_path()
    if not db.is_file() or db.is_symlink():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM series WHERE provider=? AND frequency='1d' "
            "AND adjustment='raw_unadjusted' ORDER BY symbol", (provider,)
        )]
    finally:
        con.close()


def latest_sessions(provider: str) -> dict[str, date]:
    """Read maximum stored trading session per symbol in one indexed query."""
    db = database_path()
    if not db.is_file() or db.is_symlink():
        return {}
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {
            symbol: date.fromisoformat(day)
            for symbol, day in con.execute(
                "SELECT s.symbol, MAX(p.session_date) FROM series s "
                "JOIN daily_prices p ON p.series_id=s.id "
                "WHERE s.provider=? AND s.frequency='1d' "
                "AND s.adjustment='raw_unadjusted' GROUP BY s.symbol", (provider,)
            )
        }
    finally:
        con.close()


def run_cycle(*, provider: str, state_path: Path, max_symbols: int = 0,
              delay_seconds: float = 0, indexes_only: bool = False) -> dict:
    state = status(state_path)
    checkpoint = {
        "status": "running", "started_at": now_utc().isoformat(),
        "provider": provider, "indexes": {}, "equities": {},
        "completed": 0, "failed": 0, "last_completed_date": state.get("last_completed_date"),
    }
    save(state_path, checkpoint)
    for code in INDEX_SERIES:
        try:
            result = acquire_index_closes(code)
            checkpoint["indexes"][code] = {
                "status": "stored", "inserted": result["inserted"],
                "updated": result["updated"], "last_session": result["last_session"]
            }
            checkpoint["completed"] += 1
        except Exception as exc:
            checkpoint["indexes"][code] = {"status": "failed", "error_type": type(exc).__name__}
            checkpoint["failed"] += 1
        save(state_path, checkpoint)
    if not indexes_only:
        symbols = instruments(provider)
        if max_symbols:
            symbols = symbols[:max_symbols]
        checkpoints = latest_sessions(provider)
        checkpoint["total_equities"] = len(symbols)
        for idx, symbol in enumerate(symbols):
            if idx and delay_seconds:
                time.sleep(delay_seconds)
            checkpoint["current_symbol"] = symbol
            save(state_path, checkpoint)
            try:
                # Reconcile overlapping sessions to capture late revisions and holidays.
                last = checkpoints.get(symbol)
                start = last - timedelta(days=8) if last else None
                result = acquire_and_store(
                    symbol, provider=provider, period="5y",
                    start_date=start if provider != "alpha_vantage" else None,
                )
                checkpoint["equities"][symbol] = {
                    "status": "stored", "inserted": result["inserted"],
                    "updated": result["updated"], "last_session": result["last_session"],
                }
                checkpoint["completed"] += 1
            except ProviderRateLimited:
                checkpoint["status"] = "rate_limited"
                checkpoint["current_symbol"] = None
                save(state_path, checkpoint)
                return checkpoint
            except (ProviderError, OSError, ValueError) as exc:
                checkpoint["equities"][symbol] = {
                    "status": "failed", "error_type": type(exc).__name__
                }
                checkpoint["failed"] += 1
            save(state_path, checkpoint)
    checkpoint["current_symbol"] = None
    checkpoint["finished_at"] = now_utc().isoformat()
    checkpoint["status"] = "complete" if not checkpoint["failed"] else "partial"
    if not checkpoint["failed"]:
        checkpoint["last_completed_date"] = now_utc().astimezone(ET).date().isoformat()
    save(state_path, checkpoint)
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinIntel local daily refresh worker")
    parser.add_argument("--provider", choices=("alpaca_sip", "tiingo", "alpha_vantage"),
                        default="alpaca_sip")
    parser.add_argument("--state-path", type=Path, default=STATE)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--delay-seconds", type=float, default=1)
    parser.add_argument("--indexes-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.max_symbols < 0 or args.delay_seconds < 0:
        parser.error("max-symbols and delay-seconds must be nonnegative")
    if args.status:
        print(json.dumps(status(args.state_path)))
        return 0
    if args.once:
        result = run_cycle(provider=args.provider, state_path=args.state_path,
                           max_symbols=args.max_symbols,
                           delay_seconds=args.delay_seconds,
                           indexes_only=args.indexes_only)
        print(json.dumps({k: v for k, v in result.items() if k != "equities"}))
        return int(result["status"] != "complete")
    # Dedicated, local process: idempotent for completed date; catch up on restart.
    while True:
        eligible = due_date(now_utc())
        previous = status(args.state_path)
        last_attempt = previous.get("started_at", "")
        # Failed runs retry no more than hourly, preventing a tight request loop.
        recent = False
        if last_attempt:
            try:
                recent = (now_utc() - datetime.fromisoformat(last_attempt)).total_seconds() < 3600
            except ValueError:
                pass
        if eligible and previous.get("last_completed_date") != eligible and not recent:
            run_cycle(provider=args.provider, state_path=args.state_path,
                      max_symbols=args.max_symbols,
                      delay_seconds=args.delay_seconds, indexes_only=args.indexes_only)
        time.sleep(300)


if __name__ == "__main__":
    raise SystemExit(main())
