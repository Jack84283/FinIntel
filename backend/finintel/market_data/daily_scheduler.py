"""Calendar-aware, durable daily market-data acquisition.

The scheduler is safe to start with the FastAPI sidecar. It never starts a
provider request merely because this module was imported; the application
startup hook owns the background thread and automatic acquisition is disabled
until the user enables it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from finintel.market_data.acquisition import acquire_and_store
from finintel.market_data.daily_acquisition_store import (
    PROVIDERS,
    append_provider_error,
    cancel_job,
    claim_job,
    create_job,
    finish_job,
    get_active_job,
    get_job,
    get_settings,
    heartbeat,
    job_control,
    record_item,
    release_lock,
    resumable_job,
    retry_failed_job,
    set_current_instrument,
    set_item_running,
    set_job_pause,
    update_settings,
)
from finintel.market_data.index_history import (
    INDEX_SERIES,
    IndexHistoryError,
    acquire_index_closes,
)
from finintel.market_data.provider import ProviderError, ProviderRateLimited, ProviderUnavailable
from finintel.storage.db import StorageError, database_path

ET = ZoneInfo("America/New_York")
STATE = Path("data/daily-acquisition.json")
RETRYABLE_ERRORS = (ProviderRateLimited, ProviderUnavailable, OSError)
_CALENDAR = xcals.get_calendar("XNYS")


def now_utc() -> datetime:
    return datetime.now(UTC)


def _local_now(now: datetime, timezone: str) -> datetime:
    return now.astimezone(ZoneInfo(timezone))


def _schedule_datetime(day: date, schedule_time: str, timezone: str) -> datetime:
    hour, minute = (int(part) for part in schedule_time.split(":", 1))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(timezone))


def is_trading_day(day: date) -> bool:
    """Return whether XNYS has a regular session for ``day``."""
    return bool(_CALENDAR.is_session(pd.Timestamp(day)))


def trading_sessions(start: date, end: date) -> list[date]:
    if start > end:
        return []
    sessions = _CALENDAR.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [session.date() for session in sessions]


def next_trading_day(day: date) -> date:
    sessions = _CALENDAR.sessions_in_range(
        pd.Timestamp(day + timedelta(days=1)), pd.Timestamp(day + timedelta(days=370))
    )
    if len(sessions) == 0:
        raise RuntimeError("NYSE calendar did not provide a future trading session")
    return sessions[0].date()


def due_date(
    now: datetime,
    schedule_time: str = "20:00",
    timezone: str = "America/New_York",
) -> str | None:
    """Return today's trading date after the configured local schedule time."""
    local = _local_now(now, timezone)
    if not is_trading_day(local.date()):
        return None
    scheduled = _schedule_datetime(local.date(), schedule_time, timezone)
    return local.date().isoformat() if local >= scheduled else None


def next_scheduled_execution(
    now: datetime,
    schedule_time: str = "20:00",
    timezone: str = "America/New_York",
) -> datetime:
    local = _local_now(now, timezone)
    if is_trading_day(local.date()):
        scheduled = _schedule_datetime(local.date(), schedule_time, timezone)
        if local < scheduled:
            return scheduled.astimezone(UTC)
    return _schedule_datetime(next_trading_day(local.date()), schedule_time, timezone).astimezone(
        UTC
    )


def expected_missing_sessions(start: date | None, end: date | None) -> int:
    if start is None or end is None or start >= end:
        return 0
    return max(0, len(trading_sessions(start + timedelta(days=1), end)))


def status(path: Path = STATE) -> dict:
    """Read the legacy JSON checkpoint used by older developer workflows."""
    if not path.exists():
        return {"status": "never_run", "last_completed_date": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, state: dict) -> None:
    """Atomically preserve the legacy checkpoint for CLI compatibility."""
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
        return [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT symbol FROM series WHERE provider=? AND frequency='1d' "
                "AND adjustment='raw_unadjusted' ORDER BY symbol",
                (provider,),
            )
        ]
    finally:
        con.close()


def latest_sessions(provider: str) -> dict[str, date]:
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
                "AND s.adjustment='raw_unadjusted' GROUP BY s.symbol",
                (provider,),
            )
        }
    finally:
        con.close()


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"code": type(exc).__name__, "message": str(exc)[:240]}


class DailyAcquisitionScheduler:
    """One process-local worker backed by a cross-process SQLite lease."""

    def __init__(self, *, poll_seconds: float = 30.0) -> None:
        self._poll_seconds = max(1.0, poll_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="finintel-daily-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    def trigger_manual(self) -> dict[str, object]:
        settings = get_settings()
        active = get_active_job()
        if active is not None:
            raise RuntimeError("A daily acquisition job is already active")
        job = create_job(
            kind="manual",
            provider=str(settings["primary_provider"]),
            secondary_provider=settings["secondary_provider"],
            scheduled_for=now_utc().isoformat(timespec="seconds"),
            schedule_date=None,
            equity_symbols=instruments(str(settings["primary_provider"])),
            index_codes=list(INDEX_SERIES),
        )
        self._launch(str(job["id"]))
        self._wake.set()
        return get_job(str(job["id"])) or job

    def pause(self, job_id: str) -> dict[str, object] | None:
        result = set_job_pause(job_id, True)
        self._wake.set()
        return result

    def resume(self, job_id: str) -> dict[str, object] | None:
        result = set_job_pause(job_id, False)
        if result:
            self._launch(job_id)
        self._wake.set()
        return result

    def cancel(self, job_id: str) -> dict[str, object] | None:
        result = cancel_job(job_id)
        self._wake.set()
        return result

    def retry_failed(self, job_id: str) -> dict[str, object] | None:
        result = retry_failed_job(job_id)
        if result and result.get("id"):
            self._launch(str(result["id"]))
        self._wake.set()
        return result

    def update_settings(self, values: dict[str, object]) -> dict[str, object]:
        result = update_settings(values)
        self._wake.set()
        return result

    def _loop(self) -> None:
        self._resume_interrupted()
        while not self._stop.is_set():
            try:
                settings = get_settings()
                active = get_active_job()
                if active and active["status"] in ("scheduled", "running"):
                    self._launch(str(active["id"]))
                elif settings["enabled"]:
                    scheduled_date = due_date(
                        now_utc(), str(settings["schedule_time"]), str(settings["timezone"])
                    )
                    if scheduled_date:
                        job = create_job(
                            kind="automatic",
                            provider=str(settings["primary_provider"]),
                            secondary_provider=settings["secondary_provider"],
                            scheduled_for=_schedule_datetime(
                                date.fromisoformat(scheduled_date),
                                str(settings["schedule_time"]),
                                str(settings["timezone"]),
                            )
                            .astimezone(UTC)
                            .isoformat(timespec="seconds"),
                            schedule_date=scheduled_date,
                            equity_symbols=instruments(str(settings["primary_provider"])),
                            index_codes=list(INDEX_SERIES),
                        )
                        self._launch(str(job["id"]))
            except (OSError, StorageError, ValueError, RuntimeError):
                pass
            self._wake.wait(self._poll_seconds)
            self._wake.clear()

    def _resume_interrupted(self) -> None:
        try:
            job = resumable_job()
            if job:
                self._launch(str(job["id"]))
        except (OSError, StorageError, ValueError):
            pass

    def _launch(self, job_id: str) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_job,
                args=(job_id,),
                name="finintel-daily-acquisition",
                daemon=True,
            )
            self._worker.start()

    def run_job_blocking(self, job_id: str) -> dict[str, object] | None:
        self._run_job(job_id)
        return get_job(job_id)

    def _run_job(self, job_id: str) -> None:
        owner = uuid.uuid4().hex
        try:
            if not claim_job(job_id, owner):
                return
            settings = get_settings()
            job = get_job(job_id, include_items=True)
            if not job:
                return
            latest = latest_sessions(str(job["provider"]))
            for item in job.get("items", []):
                if item["status"] == "completed":
                    continue
                if self._should_stop(job_id):
                    return
                set_current_instrument(job_id, item["code"])
                if item["kind"] == "index":
                    self._run_index(job_id, item["code"], settings)
                else:
                    self._run_equity(
                        job_id,
                        item["code"],
                        str(job["provider"]),
                        job.get("secondary_provider"),
                        settings,
                        latest,
                    )
                heartbeat(job_id, owner)
            if not self._should_stop(job_id, terminal_only=True):
                finish_job(job_id)
        except (OSError, StorageError, ValueError, RuntimeError) as exc:
            append_provider_error(job_id, _safe_error(exc))
            finish_job(job_id, status="failed")
        finally:
            release_lock(job_id, owner)

    def _should_stop(self, job_id: str, *, terminal_only: bool = False) -> bool:
        control = job_control(job_id)
        if not control:
            return True
        if control["cancel_requested"] or (control["terminal"] and not terminal_only):
            return True
        if control["pause_requested"]:
            while not self._stop.is_set():
                current = job_control(job_id)
                if not current or current["cancel_requested"] or current["terminal"]:
                    return True
                if not current["pause_requested"]:
                    return False
                self._wake.wait(0.5)
                self._wake.clear()
            return True
        return False

    def _wait_retry(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(min(0.5, max(0.01, deadline - time.monotonic())))
        return not self._stop.is_set()

    def _run_index(self, job_id: str, code: str, settings: dict[str, object]) -> None:
        attempts = int(settings["retry_attempts"])
        backoff = float(settings["retry_backoff_seconds"])
        last_error: BaseException | None = None
        for attempt in range(attempts):
            set_item_running(job_id, "index", code)
            try:
                result = acquire_index_closes(code)
                record_item(
                    job_id,
                    "index",
                    code,
                    status="completed",
                    provider="fred",
                    last_session=str(result["last_session"]),
                    inserted=int(result["inserted"]),
                    updated=int(result["updated"]),
                    unchanged=int(result["unchanged"]),
                    validation_status="validated",
                    index_result=result,
                )
                return
            except (IndexHistoryError, StorageError, OSError) as exc:
                last_error = exc
                append_provider_error(job_id, {**_safe_error(exc), "instrument": code})
                if attempt + 1 < attempts and self._wait_retry(backoff * (2**attempt)):
                    continue
        record_item(
            job_id,
            "index",
            code,
            status="failed",
            provider="fred",
            error_type=type(last_error).__name__ if last_error else "IndexHistoryError",
            error_message=str(last_error) if last_error else "Index refresh failed",
            validation_status="failed",
        )

    def _run_equity(
        self,
        job_id: str,
        symbol: str,
        provider: str,
        secondary_provider: object,
        settings: dict[str, object],
        latest: dict[str, date],
    ) -> None:
        providers = [provider]
        if (
            isinstance(secondary_provider, str)
            and secondary_provider in PROVIDERS
            and secondary_provider not in providers
        ):
            providers.append(secondary_provider)
        attempts = int(settings["retry_attempts"])
        backoff = float(settings["retry_backoff_seconds"])
        overlap = int(settings["overlap_days"])
        last_session = latest.get(symbol)
        start = last_session - timedelta(days=overlap) if last_session and overlap else last_session
        last_error: BaseException | None = None
        for selected in providers:
            for attempt in range(attempts):
                if self._should_stop(job_id):
                    return
                set_item_running(job_id, "equity", symbol)
                try:
                    result = acquire_and_store(
                        symbol,
                        provider=selected,
                        period="5y",
                        start_date=start if selected != "alpha_vantage" else None,
                    )
                    missing = expected_missing_sessions(
                        last_session, date.fromisoformat(str(result["last_session"]))
                    )
                    record_item(
                        job_id,
                        "equity",
                        symbol,
                        status="completed",
                        provider=selected,
                        last_session=str(result["last_session"]),
                        inserted=int(result["inserted"]),
                        updated=int(result["updated"]),
                        unchanged=int(result["unchanged"]),
                        stale=int(result["stale"]),
                        validation_status="validated" if missing == 0 else "incomplete",
                        missing_sessions=missing,
                    )
                    return
                except (ProviderError, StorageError, OSError, ValueError) as exc:
                    last_error = exc
                    append_provider_error(
                        job_id,
                        {**_safe_error(exc), "instrument": symbol, "provider": selected},
                    )
                    if (
                        isinstance(exc, RETRYABLE_ERRORS)
                        and attempt + 1 < attempts
                        and self._wait_retry(backoff * (2**attempt))
                    ):
                        continue
                    break
        record_item(
            job_id,
            "equity",
            symbol,
            status="failed",
            provider=providers[-1],
            error_type=type(last_error).__name__ if last_error else "ProviderError",
            error_message=str(last_error) if last_error else "Acquisition failed",
            validation_status="failed",
        )


def run_cycle(
    *,
    provider: str,
    state_path: Path,
    max_symbols: int = 0,
    delay_seconds: float = 0,
    indexes_only: bool = False,
) -> dict:
    """Run one durable manual cycle for the legacy CLI entry point."""
    if provider not in PROVIDERS:
        raise ValueError("Unsupported acquisition provider")
    symbols = instruments(provider)
    if max_symbols:
        symbols = symbols[:max_symbols]
    job = create_job(
        kind="manual",
        provider=provider,
        secondary_provider=None,
        scheduled_for=now_utc().isoformat(timespec="seconds"),
        schedule_date=None,
        equity_symbols=[] if indexes_only else symbols,
        index_codes=list(INDEX_SERIES),
    )
    scheduler = DailyAcquisitionScheduler(poll_seconds=max(1, delay_seconds or 1))
    result = scheduler.run_job_blocking(str(job["id"])) or {}
    if state_path:
        save(
            state_path,
            {
                "status": result.get("status", "failed"),
                "provider": provider,
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "completed": result.get("completed_instruments", 0),
                "failed": result.get("failed_instruments", 0),
                "total_equities": len(symbols),
                "current_symbol": result.get("current_instrument"),
                "last_completed_date": (
                    now_utc().astimezone(ET).date().isoformat()
                    if result.get("status") == "completed"
                    else None
                ),
                "indexes": result.get("index_results", {}),
            },
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinIntel local daily refresh worker")
    parser.add_argument("--provider", choices=PROVIDERS, default="alpaca_sip")
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
        result = run_cycle(
            provider=args.provider,
            state_path=args.state_path,
            max_symbols=args.max_symbols,
            delay_seconds=args.delay_seconds,
            indexes_only=args.indexes_only,
        )
        print(json.dumps(result))
        return int(result.get("status") not in ("completed", "partial"))
    scheduler = DailyAcquisitionScheduler(poll_seconds=300)
    scheduler.start()
    try:
        while True:
            time.sleep(300)
    except KeyboardInterrupt:
        scheduler.stop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
