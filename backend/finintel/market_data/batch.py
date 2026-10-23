"""Explicit, bounded catalog acquisition run for a separate local process.

Run from the repository root with the backend virtualenv activated.
This is intentionally not an API endpoint, scheduler, or automatic task.
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime

from finintel.market_data.acquisition import acquire_and_store
from finintel.market_data.provider import ProviderError, ProviderRateLimited
from finintel.storage.db import StorageError, database_path


def pending_symbols(*, provider: str, maximum: int, asset_type: str = "all") -> list[str]:
    """Read only listed, supported symbols without creating a database."""
    path = database_path()
    if not path.is_file() or path.is_symlink():
        raise StorageError("Initialize the local symbol catalog first from Data Acquisition")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            raise StorageError("Local catalog needs a schema upgrade")
        kinds = ("Stock", "ETF") if asset_type == "all" else (asset_type,)
        placeholders = ",".join("?" for _ in kinds)
        rows = conn.execute(
            "SELECT c.symbol FROM symbol_catalog AS c "
            "WHERE c.asset_type IN (" + placeholders + ") "
            "AND NOT EXISTS (SELECT 1 FROM series AS s WHERE s.symbol=c.symbol AND "
            "s.provider=? AND s.frequency='1d' AND s.adjustment='raw_unadjusted') "
            "ORDER BY c.symbol LIMIT ?",
            (*kinds, provider, maximum),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire missing local daily history, one symbol at a time"
    )
    parser.add_argument(
        "--provider", choices=("alpha_vantage", "alpaca_sip"), default="alpha_vantage"
    )
    parser.add_argument("--asset-type", choices=("all", "Stock", "ETF"), default="all")
    parser.add_argument(
        "--max-requests", type=int, default=20, help="Hard per-run cap; defaults to 20"
    )
    parser.add_argument(
        "--delay-seconds", type=float, default=15, help="Delay between provider calls"
    )
    parser.add_argument("--outputsize", choices=("compact", "full"), default="compact")
    parser.add_argument("--period", choices=("5y", "10y", "max"), default="10y")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 500:
        parser.error("--max-requests must be from 1 to 500")
    if not 0 <= args.delay_seconds <= 3600:
        parser.error("--delay-seconds must be from 0 to 3600")
    try:
        candidates = pending_symbols(
            provider=args.provider, maximum=args.max_requests, asset_type=args.asset_type
        )
    except (StorageError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {
                    "provider": args.provider,
                    "count": len(candidates),
                    "symbols": candidates,
                    "dry_run": True,
                }
            )
        )
        return 0
    succeeded = failed = 0
    for position, symbol in enumerate(candidates):
        if position:
            time.sleep(args.delay_seconds)
        try:
            result = acquire_and_store(
                symbol, provider=args.provider, outputsize=args.outputsize, period=args.period
            )
            succeeded += 1
            print(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "symbol": symbol,
                        "status": "stored",
                        "import_id": result["import_id"],
                    }
                ),
                flush=True,
            )
        except ProviderRateLimited:
            print(
                json.dumps({"symbol": symbol, "status": "quota_reached", "stopped": True}),
                flush=True,
            )
            break
        except (ProviderError, StorageError, OSError, ValueError) as exc:
            # Restrict logged detail to type only; never print provider responses or secrets.
            failed += 1
            print(
                json.dumps(
                    {"symbol": symbol, "status": "failed", "error_type": type(exc).__name__}
                ),
                flush=True,
            )
    print(
        json.dumps(
            {
                "succeeded": succeeded,
                "failed": failed,
                "attempted": succeeded + failed,
                "selected": len(candidates),
            }
        ),
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
