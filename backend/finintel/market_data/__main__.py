"""Run a one-shot private historical-data fetch.

Example:
    FININTEL_ALPHA_VANTAGE_API_KEY=... \
      python -m finintel.market_data --symbol AAPL

PR #2 saves a local normalized JSON snapshot; PR #3 adds SQLite ingestion.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from finintel.market_data.models import HistoricalBatch
from finintel.market_data.provider import AlphaVantageDailyProvider, ProviderError


def write_snapshot(destination: Path, batch: HistoricalBatch) -> None:
    """Atomically replace only with a fully validated batch.

    A failed fetch/parse never touches a previously successful snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise OSError("Refusing to overwrite a symlink")
    fd, temporary = tempfile.mkstemp(
        prefix="." + destination.name + ".",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(batch.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch personal-use historical daily OHLCV")
    parser.add_argument("--symbol", required=True, help="Equity ticker (AAPL, MSFT, NVDA, SPY)")
    parser.add_argument(
        "--outputsize",
        choices=["compact", "full"],
        default="compact",
        help="compact: latest 100 sessions; full: premium provider entitlement only",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Local JSON snapshot path (default data/raw/<SYMBOL>.json)",
    )
    args = parser.parse_args(argv)
    symbol = args.symbol.strip().upper()
    destination = args.output or Path("data/raw") / (symbol + ".json")
    try:
        batch = AlphaVantageDailyProvider().fetch_daily(symbol, outputsize=args.outputsize)
        write_snapshot(destination, batch)
    except (ProviderError, OSError) as exc:
        print("Import failed: " + str(exc), file=sys.stderr)
        return 1

    # No API keys, provider response bodies, or private file content in logs.
    print(
        json.dumps(
            {
                "status": "validated_local_snapshot",
                "symbol": batch.symbol,
                "provider": batch.provider,
                "observations": len(batch.observations),
                "first_session": batch.first_session.isoformat(),
                "last_session": batch.last_session.isoformat(),
                "source_url": batch.source_url,
                "output": str(destination),
                "not_live_quotes": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
