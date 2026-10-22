"""One-shot import of validated PR #2 JSON snapshots into local SQLite.

Examples:
  python -m finintel.storage import data/raw/AAPL.json
  python -m finintel.storage inspect --symbol AAPL
  python -m finintel.storage imports
"""

import argparse
import json
import sys
from pathlib import Path

from finintel.storage.db import (
    StorageError,
    database_path,
    import_snapshot,
    list_imports,
    list_instruments,
    read_daily,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage locally persisted market history")
    parser.add_argument("--db", type=Path, default=None, help="Local SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd_import = commands.add_parser("import", help="Validate and import one provider snapshot")
    cmd_import.add_argument("snapshot", type=Path)
    cmd_inspect = commands.add_parser("inspect", help="Inspect stored instruments or daily prices")
    cmd_inspect.add_argument("--symbol", help="Equity symbol to inspect")
    cmd_inspect.add_argument("--limit", type=int, default=20, help="Maximum daily observations")
    cmd_runs = commands.add_parser("imports", help="List recent local ingestion runs")
    cmd_runs.add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)
    path = args.db if args.db is not None else database_path()
    try:
        if args.command == "import":
            result = import_snapshot(args.snapshot, db=path)
        elif args.command == "inspect":
            if args.symbol:
                result = read_daily(args.symbol, db=path, limit=args.limit)
                if result is None:
                    raise StorageError("No stored daily data for this symbol")
            else:
                result = list_instruments(db=path)
        else:
            result = list_imports(db=path, limit=args.limit)
        print(json.dumps(result, indent=2))
    except (StorageError, ValueError) as exc:
        print("Storage operation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
