"""Explicit, resumable FRED index backfill with bounded request windows.

The worker is opt-in and never starts from the application lifecycle. It uses
the shared FRED acquisition path, so every chunk is validated and every HTTP
request is reserved through the local provider pacing and budget accounting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from finintel.market_data.index_history import (
    INDEX_SERIES,
    IndexHistoryError,
    acquire_index_closes,
)
from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from finintel.storage.db import StorageError, database_path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


CHECKPOINT_VERSION = 1
DEFAULT_START = date(1776, 1, 1)
DEFAULT_CHUNK_DAYS = 3650
MAX_CHUNK_DAYS = 3650


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Dates must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Dates must use YYYY-MM-DD")
    return parsed


def _validate_range(start: date, end: date) -> None:
    today = datetime.now(UTC).date()
    if start > end:
        raise ValueError("Index backfill start date must not be after the end date")
    if end > today:
        raise ValueError("Index backfill dates cannot be in the future")


def _save(path: Path, state: dict[str, object]) -> None:
    if path.is_symlink():
        raise OSError("Refusing to overwrite a symlink checkpoint")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(
    path: Path,
    *,
    code: str,
    start: date,
    end: date,
    chunk_days: int,
) -> dict[str, object]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise TypeError("Index backfill checkpoint must contain an object")
        expected = {
            "version": CHECKPOINT_VERSION,
            "code": code,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "chunk_days": chunk_days,
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing index backfill checkpoint does not match this request")
        if not isinstance(state.get("completed_ranges", []), list):
            raise TypeError("Index backfill checkpoint has invalid completed ranges")
        try:
            date.fromisoformat(str(state.get("next_start", start.isoformat())))
        except ValueError as exc:
            raise TypeError("Index backfill checkpoint has an invalid next date") from exc
        return state
    return {
        "version": CHECKPOINT_VERSION,
        "provider": "fred",
        "code": code,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "chunk_days": chunk_days,
        "next_start": start.isoformat(),
        "completed_ranges": [],
        "last_status": "created",
    }


def _acquire_checkpoint_lock(handle) -> bool:
    if os.name == "nt":
        handle.seek(0)
        handle.write("0")
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _next_end(start: date, end: date, chunk_days: int) -> date:
    return min(end, start + timedelta(days=chunk_days - 1))


def run(
    *,
    code: str,
    state_path: Path,
    db: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    once: bool = False,
    acquire: Callable[..., dict[str, object]] = acquire_index_closes,
) -> int:
    if code not in INDEX_SERIES:
        raise ValueError("Unsupported index code")
    if not 1 <= chunk_days <= MAX_CHUNK_DAYS:
        raise ValueError(f"chunk_days must be between 1 and {MAX_CHUNK_DAYS}")
    first = start or DEFAULT_START
    last = end or datetime.now(UTC).date()
    _validate_range(first, last)

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        if not _acquire_checkpoint_lock(lock):
            print("Another index backfill is already using this checkpoint.", file=sys.stderr)
            return 2
        state = _load(
            state_path,
            code=code,
            start=first,
            end=last,
            chunk_days=chunk_days,
        )
        while True:
            next_start = date.fromisoformat(str(state["next_start"]))
            if next_start > last:
                state["last_status"] = "complete"
                state["current_range"] = None
                _save(state_path, state)
                print(json.dumps({"code": code, "status": "complete"}), flush=True)
                return 0

            next_end = _next_end(next_start, last, chunk_days)
            state["current_range"] = {
                "start": next_start.isoformat(),
                "end": next_end.isoformat(),
            }
            state["last_status"] = "running"
            _save(state_path, state)
            try:
                result = acquire(
                    code,
                    db=db,
                    start=next_start,
                    end=next_end,
                    allow_empty=True,
                )
            except (ProviderRateLimited, ProviderAccessDenied, ProviderUnavailable) as exc:
                state["last_status"] = "paused_provider"
                state["last_error_type"] = type(exc).__name__
                state["last_error"] = str(exc)
                _save(state_path, state)
                return 2
            except (IndexHistoryError, StorageError, OSError, ValueError) as exc:
                state["last_status"] = "paused_error"
                state["last_error_type"] = type(exc).__name__
                state["last_error"] = str(exc)
                _save(state_path, state)
                return 2

            state["completed_ranges"].append(
                {
                    "start": next_start.isoformat(),
                    "end": next_end.isoformat(),
                    "status": result.get("status"),
                    "sessions": result.get("sessions", 0),
                    "inserted": result.get("inserted", 0),
                    "updated": result.get("updated", 0),
                    "unchanged": result.get("unchanged", 0),
                }
            )
            state["next_start"] = (next_end + timedelta(days=1)).isoformat()
            state["current_range"] = None
            state["last_status"] = "stored" if result.get("status") == "stored" else "empty"
            state.pop("last_error", None)
            state.pop("last_error_type", None)
            _save(state_path, state)
            print(
                json.dumps(
                    {
                        "code": code,
                        "status": state["last_status"],
                        "start": next_start.isoformat(),
                        "end": next_end.isoformat(),
                        "sessions": result.get("sessions", 0),
                    }
                ),
                flush=True,
            )
            if once:
                return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resumable bounded FRED index history backfill")
    parser.add_argument("--code", choices=tuple(INDEX_SERIES), required=True)
    parser.add_argument("--start", type=_parse_date, default=None)
    parser.add_argument("--end", type=_parse_date, default=None)
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=DEFAULT_CHUNK_DAYS,
        help="Maximum calendar days per provider request (default: 3650)",
    )
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="Process one chunk and exit")
    args = parser.parse_args(argv)
    state_path = args.state_path or database_path().with_suffix(
        "." + args.code.lower() + ".index-backfill.json"
    )
    try:
        return run(
            code=args.code,
            state_path=state_path,
            db=args.db,
            start=args.start,
            end=args.end,
            chunk_days=args.chunk_days,
            once=args.once,
        )
    except (IndexHistoryError, ProviderError, StorageError, OSError, TypeError, ValueError) as exc:
        print("Index backfill stopped: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
