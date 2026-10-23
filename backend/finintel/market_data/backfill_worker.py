"""Explicit long-running local historical backfill with durable daily checkpoints.

Start once in a detached local process; does not run on import or app launch.
Successful imports are skipped through the shared local SQLite catalog.
"""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from finintel.market_data.acquisition import acquire_and_store, key_configured
from finintel.market_data.alpaca import alpaca_configured
from finintel.market_data.batch import pending_symbols
from finintel.market_data.provider import ProviderError, ProviderRateLimited
from finintel.market_data.tiingo import tiingo_configured
from finintel.storage.db import StorageError, database_path


def _day() -> str:
    return datetime.now(UTC).date().isoformat()


def _save(path: Path, state: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path, *, provider: str, asset_type: str) -> dict[str, object]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("provider") != provider or state.get("asset_type") != asset_type:
            raise ValueError("Existing checkpoint belongs to another provider or universe")
    else:
        state = {"provider": provider, "asset_type": asset_type}
    if state.get("day") != _day():
        state.update(day=_day(), requests=0, attempted=[], last_status="resumed")
    return state


def _sleep_until_next_utc_day() -> None:
    now = datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    time.sleep(max(1, (tomorrow - now).total_seconds() + 30))


def run(
    *,
    provider: str,
    asset_type: str,
    max_per_day: int,
    delay: float,
    state_path: Path,
    period: str = "10y",
    outputsize: str = "compact",
    once: bool = False,
) -> int:
    if provider == "alpaca_sip" and not alpaca_configured():
        print("Alpaca API credentials are not configured; no requests were sent.", file=sys.stderr)
        return 2
    if provider == "alpha_vantage" and not key_configured():
        print("Alpha Vantage API key is not configured; no requests were sent.", file=sys.stderr)
        return 2
    if provider == "tiingo" and not tiingo_configured():
        print("Tiingo token is not configured; no requests were sent.", file=sys.stderr)
        return 2
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another historical backfill is already using this checkpoint.", file=sys.stderr)
            return 2
        consecutive_provider_errors = 0
        while True:
            state = _load(state_path, provider=provider, asset_type=asset_type)
            attempted = set(state["attempted"])
            if int(state["requests"]) >= max_per_day:
                state["last_status"] = "daily_limit"
                _save(state_path, state)
                if once:
                    return 0
                _sleep_until_next_utc_day()
                continue
            candidates = pending_symbols(provider=provider, maximum=100000, asset_type=asset_type)
            remaining = [symbol for symbol in candidates if symbol not in attempted]
            if not remaining:
                state["last_status"] = "complete" if not candidates else "retry_next_day"
                _save(state_path, state)
                if not candidates or once:
                    return 0
                _sleep_until_next_utc_day()
                continue
            symbol = remaining[0]
            # Persist reservation BEFORE calling the provider: crashes cannot reset the quota.
            state["requests"] = int(state["requests"]) + 1
            state["attempted"] = sorted((*attempted, symbol))
            state["current_symbol"] = symbol
            state["last_status"] = "running"
            _save(state_path, state)
            try:
                result = acquire_and_store(
                    symbol, provider=provider, outputsize=outputsize, period=period
                )
                consecutive_provider_errors = 0
                state["last_status"] = "stored"
                state["last_import_id"] = result["import_id"]
                print(json.dumps({"symbol": symbol, "status": "stored"}), flush=True)
            except ProviderRateLimited:
                state["last_status"] = "rate_limited"
                print(json.dumps({"symbol": symbol, "status": "rate_limited"}), flush=True)
                _save(state_path, state)
                if once:
                    return 0
                _sleep_until_next_utc_day()
                continue
            except ProviderError as exc:
                consecutive_provider_errors += 1
                state["last_status"] = "provider_error"
                state["last_error_type"] = type(exc).__name__
                print(
                    json.dumps(
                        {
                            "symbol": symbol,
                            "status": "provider_error",
                            "consecutive": consecutive_provider_errors,
                        }
                    ),
                    flush=True,
                )
                if consecutive_provider_errors >= 3:
                    state["last_status"] = "paused_provider_errors"
                    print(
                        "Stopped after three consecutive provider errors. Check credentials and feed entitlement.",
                        file=sys.stderr,
                    )
                    return 2
            except (StorageError, OSError, ValueError) as exc:
                state["last_status"] = "failed"
                state["last_error_type"] = type(exc).__name__
                print(
                    json.dumps(
                        {"symbol": symbol, "status": "failed", "error_type": type(exc).__name__}
                    ),
                    flush=True,
                )
            finally:
                state["current_symbol"] = None
                _save(state_path, state)
            if delay:
                time.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent local historical backfill worker")
    parser.add_argument(
        "--provider", choices=("alpaca_sip", "alpha_vantage", "tiingo"), default="alpaca_sip"
    )
    parser.add_argument("--asset-type", choices=("all", "Stock", "ETF"), default="all")
    parser.add_argument("--max-requests-per-day", type=int, default=20)
    parser.add_argument("--delay-seconds", type=float, default=15.0)
    parser.add_argument("--period", choices=("5y", "10y", "max"), default="10y")
    parser.add_argument("--outputsize", choices=("compact", "full"), default="compact")
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests_per_day <= 100000:
        parser.error("Daily request cap must be 1–100000")
    if not 0 <= args.delay_seconds <= 3600:
        parser.error("Delay must be 0–3600 seconds")
    path = args.state_path or database_path().with_suffix(".backfill.json")
    try:
        return run(
            provider=args.provider,
            asset_type=args.asset_type,
            max_per_day=args.max_requests_per_day,
            delay=args.delay_seconds,
            state_path=path,
            period=args.period,
            outputsize=args.outputsize,
            once=args.once,
        )
    except (StorageError, sqlite3.Error, ValueError, OSError) as exc:
        print("Backfill stopped: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
