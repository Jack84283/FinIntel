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
    LeaseLostError,
    append_provider_error,
    cancel_job,
    claim_job,
    create_job,
    finish_job,
    get_active_job,
    get_automatic_job,
    get_job,
    get_settings,
    heartbeat,
    job_control,
    list_automatic_jobs,
    mark_provider_status,
    merge_job_items,
    provider_usage_status,
    record_item,
    release_lock,
    requeue_job,
    reserve_provider_request,
    reset_provider_status,
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
from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from finintel.storage.db import StorageError, database_path

ET = ZoneInfo("America/New_York")
STATE = Path("data/daily-acquisition.json")
RETRYABLE_ERRORS = (ProviderUnavailable, OSError)
_CALENDAR = xcals.get_calendar("XNYS")
LEASE_SECONDS = 120
# Provider EOD feeds have different publication guarantees. A one-session
# grace period keeps a normal provider publication delay from becoming a false
# incomplete result, while still rejecting older/stale responses.
PROVIDER_PUBLICATION_DELAY_SESSIONS = {
    "alpaca_sip": 0,
    "tiingo": 1,
    "alpha_vantage": 1,
}


class _AcquisitionStopped(Exception):
    """Stop before an HTTP request when a scheduled job is paused or cancelled."""

    def __init__(self, *, shutdown: bool = False) -> None:
        super().__init__()
        self.shutdown = shutdown


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
    start = day + timedelta(days=1)
    end = min(day + timedelta(days=370), _CALENDAR.last_session.date())
    if start > end:
        raise RuntimeError("NYSE calendar did not provide a future trading session")
    sessions = _CALENDAR.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
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


def _latest_trading_session_on_or_before(day: date) -> date | None:
    start = max(day - timedelta(days=370), _CALENDAR.first_session.date())
    end = min(day, _CALENDAR.last_session.date())
    if start > end:
        return None
    sessions = _CALENDAR.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return sessions[-1].date() if len(sessions) else None


def latest_expected_session(now: datetime | None = None) -> date | None:
    """Return the latest NYSE session whose regular close has passed."""
    local = (now or now_utc()).astimezone(ET)
    upper = local.date()
    if is_trading_day(upper) and local < _schedule_datetime(upper, "16:00", ET.key):
        upper -= timedelta(days=1)
    return _latest_trading_session_on_or_before(upper)


def latest_recovery_session(now: datetime, schedule_time: str, timezone: str) -> date | None:
    """Return the latest session eligible for overdue work before today's run."""
    local = now.astimezone(ZoneInfo(timezone))
    if is_trading_day(local.date()) and local < _schedule_datetime(
        local.date(), schedule_time, timezone
    ):
        return _latest_trading_session_on_or_before(local.date() - timedelta(days=1))
    return latest_expected_session(now)


def latest_provider_eligible_session(provider: str, reference_date: date) -> date | None:
    """Return the latest session expected to be published by ``provider``."""
    delay = PROVIDER_PUBLICATION_DELAY_SESSIONS.get(provider, 0)
    sessions = trading_sessions(
        max(reference_date - timedelta(days=370), _CALENDAR.first_session.date()),
        min(reference_date, _CALENDAR.last_session.date()),
    )
    if len(sessions) <= delay:
        return None
    return sessions[-1 - delay]


def expected_missing_sessions(
    start: date | None,
    end: date | None,
    stored_sessions: set[date] | None = None,
) -> int:
    if start is None or end is None or start >= end:
        return 0
    expected = set(trading_sessions(start + timedelta(days=1), end))
    if stored_sessions is not None:
        expected.difference_update(stored_sessions)
    return len(expected)


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


def stored_sessions(
    symbol: str,
    provider: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> set[date]:
    """Return only the stored session-date column for one provider series."""
    db = database_path()
    if not db.is_file() or db.is_symlink():
        return set()
    clauses = [
        "s.symbol=?",
        "s.provider=?",
        "s.frequency='1d'",
        "s.adjustment='raw_unadjusted'",
    ]
    parameters: list[object] = [symbol, provider]
    if start is not None:
        clauses.append("p.session_date>=?")
        parameters.append(start.isoformat())
    if end is not None:
        clauses.append("p.session_date<=?")
        parameters.append(end.isoformat())
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {
            date.fromisoformat(row[0])
            for row in con.execute(
                "SELECT p.session_date FROM series s JOIN daily_prices p ON p.series_id=s.id "
                f"WHERE {' AND '.join(clauses)}",
                parameters,
            )
        }
    finally:
        con.close()


def missing_sessions_for_instrument(
    symbol: str,
    provider: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> set[date]:
    """Find calendar sessions missing from the observed provider series.

    The first observed provider session is treated as the listing boundary. This
    avoids inventing gaps before an instrument listed, while still detecting
    holes between observations and through the latest requested session.
    """
    db = database_path()
    if not db.is_file() or db.is_symlink():
        return set()
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        bounds = con.execute(
            "SELECT MIN(p.session_date), MAX(p.session_date) "
            "FROM series s JOIN daily_prices p ON p.series_id=s.id "
            "WHERE s.symbol=? AND s.provider=? AND s.frequency='1d' "
            "AND s.adjustment='raw_unadjusted'",
            (symbol, provider),
        ).fetchone()
    finally:
        con.close()
    if not bounds or bounds[0] is None or bounds[1] is None:
        return set()
    first = date.fromisoformat(bounds[0])
    last = date.fromisoformat(bounds[1])
    if start_date is not None:
        # Only validate the requested incremental window. Dates before the
        # first observed session remain unknown listing history, not proof of
        # missing observations.
        first = max(first, start_date)
    if end_date is not None:
        last = end_date
    if last < first:
        return set()
    if first >= last:
        return set()
    expected = set(trading_sessions(first, last))
    expected.difference_update(stored_sessions(symbol, provider, start=first, end=last))
    return expected


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"code": type(exc).__name__, "message": str(exc)[:240]}


def automatic_schedule_dates(
    settings: dict[str, object], *, now: datetime | None = None
) -> list[date]:
    """Return new due work plus overdue work eligible during non-trading days."""
    current = due_date(now or now_utc(), str(settings["schedule_time"]), str(settings["timezone"]))
    recovery_end = (
        date.fromisoformat(current)
        if current
        else latest_recovery_session(
            now or now_utc(), str(settings["schedule_time"]), str(settings["timezone"])
        )
    )
    if recovery_end is None:
        return []
    existing = list_automatic_jobs(through=recovery_end.isoformat())
    existing_by_date = {
        date.fromisoformat(str(job["schedule_date"])): job
        for job in existing
        if job.get("schedule_date")
    }
    actionable = {
        day
        for day, job in existing_by_date.items()
        if job["status"] in ("scheduled", "running", "paused", "partial", "failed")
    }
    completed_dates = [
        date.fromisoformat(str(job["schedule_date"]))
        for job in existing
        if job["status"] == "completed" and job.get("schedule_date")
    ]
    latest_completed = max(completed_dates, default=None)
    if latest_completed:
        first_missed = next_trading_day(latest_completed)
        missed = trading_sessions(first_missed, recovery_end)
    elif current:
        missed = [recovery_end]
    else:
        missed = []
    catch_up_limit = max(1, int(settings["catch_up_limit"]))
    actionable.update(day for day in missed if day not in existing_by_date)
    new_dates = sorted(day for day in actionable if day not in existing_by_date)
    if len(new_dates) > catch_up_limit:
        actionable.difference_update(new_dates[:-catch_up_limit])
    if current:
        # Revisit today's completed job so a newly discovered instrument is
        # added, while the unique automatic-date key prevents duplicates.
        actionable.add(recovery_end)
    return sorted(actionable)


def _ensure_automatic_job(schedule_day: date, settings: dict[str, object]) -> dict[str, object]:
    schedule_date = schedule_day.isoformat()
    job = get_automatic_job(schedule_date)
    if job is None:
        job = create_job(
            kind="automatic",
            provider=str(settings["primary_provider"]),
            secondary_provider=settings["secondary_provider"],
            scheduled_for=_schedule_datetime(
                schedule_day,
                str(settings["schedule_time"]),
                str(settings["timezone"]),
            )
            .astimezone(UTC)
            .isoformat(timespec="seconds"),
            schedule_date=schedule_date,
            equity_symbols=instruments(str(settings["primary_provider"])),
            index_codes=list(INDEX_SERIES),
        )
    elif job["status"] in ("failed", "partial"):
        job = requeue_job(str(job["id"])) or job
    return (
        merge_job_items(
            str(job["id"]),
            provider=str(settings["primary_provider"]),
            equity_symbols=instruments(str(settings["primary_provider"])),
            index_codes=list(INDEX_SERIES),
        )
        or job
    )


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
        self._reset_explicit_provider_states(settings, include_fred=True)
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
        if result:
            self._reset_explicit_provider_states(result)
        if result and result.get("id"):
            self._launch(str(result["id"]))
        self._wake.set()
        return result

    @staticmethod
    def _reset_explicit_provider_states(
        source: dict[str, object], *, include_fred: bool = False
    ) -> None:
        """Reset providers only for an explicit user-triggered recovery action."""
        valid_providers = {*PROVIDERS, "fred"}
        providers: list[str] = []
        for key in ("primary_provider", "provider"):
            value = source.get(key)
            if isinstance(value, str) and value in valid_providers:
                providers.append(value)
        if include_fred:
            providers.append("fred")
        value = source.get("secondary_provider")
        if isinstance(value, str) and value in valid_providers:
            providers.append(value)
        for item in source.get("items", []):
            if not isinstance(item, dict):
                continue
            value = item.get("provider")
            if isinstance(value, str) and value in valid_providers:
                providers.append(value)
            if item.get("kind") == "index":
                providers.append("fred")
        for provider in dict.fromkeys(providers):
            reset_provider_status(provider)

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
                if active is not None:
                    if active["status"] in ("scheduled", "running"):
                        self._launch(str(active["id"]))
                elif settings["enabled"]:
                    for scheduled_day in automatic_schedule_dates(settings):
                        job = _ensure_automatic_job(scheduled_day, settings)
                        if job["status"] in ("scheduled", "running"):
                            self._launch(str(job["id"]))
                            break
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
        lease_lost = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            if not claim_job(job_id, owner, lease_seconds=LEASE_SECONDS):
                return
            settings = get_settings()
            job = get_job(job_id, include_items=True)
            if not job:
                return
            heartbeat_thread = threading.Thread(
                target=self._lease_loop,
                args=(job_id, owner, lease_lost, heartbeat_stop),
                name="finintel-daily-lease-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            blocked_providers = {
                provider
                for provider, usage in provider_usage_status().items()
                if usage.get("quota_state")
                in {"budget_exhausted", "quota_exhausted", "auth_failed"}
            }
            latest_by_provider = {provider: latest_sessions(provider) for provider in PROVIDERS}
            latest_eligible_session = latest_expected_session()
            scheduled_session = (
                date.fromisoformat(str(job["schedule_date"])) if job.get("schedule_date") else None
            )
            expected_session = (
                min(scheduled_session, latest_eligible_session)
                if scheduled_session and latest_eligible_session
                else scheduled_session or latest_eligible_session
            )
            seen: set[tuple[str, str]] = set()
            while True:
                current = get_job(job_id, include_items=True)
                if not current:
                    return
                item = next(
                    (
                        candidate
                        for candidate in current.get("items", [])
                        if candidate["status"] != "completed"
                        and candidate["status"] != "cancelled"
                        and (candidate["kind"], candidate["code"]) not in seen
                    ),
                    None,
                )
                if item is None:
                    if self._should_stop(job_id, lease_lost=lease_lost, terminal_only=True):
                        return
                    finish_job(job_id, owner_token=owner)
                    return
                if self._should_stop(job_id, lease_lost=lease_lost):
                    return
                seen.add((item["kind"], item["code"]))
                set_current_instrument(job_id, item["code"], owner_token=owner)
                if item["kind"] == "index":
                    self._run_index(
                        job_id,
                        item["code"],
                        settings,
                        owner,
                        lease_lost,
                        blocked_providers=blocked_providers,
                    )
                else:
                    self._run_equity(
                        job_id,
                        item["code"],
                        str(job["provider"]),
                        job.get("secondary_provider"),
                        settings,
                        latest_by_provider,
                        expected_session,
                        owner,
                        lease_lost,
                        preferred_provider=item.get("provider"),
                        blocked_providers=blocked_providers,
                    )
                if lease_lost.is_set():
                    return
        except _AcquisitionStopped:
            # A scheduler shutdown must leave unfinished work resumable.
            return
        except LeaseLostError:
            return
        except (OSError, StorageError, ValueError, RuntimeError) as exc:
            if lease_lost.is_set():
                return
            try:
                append_provider_error(job_id, _safe_error(exc), owner_token=owner)
                finish_job(job_id, status="failed", owner_token=owner)
            except LeaseLostError:
                return
        finally:
            heartbeat_stop.set()
            if heartbeat_thread and heartbeat_thread is not threading.current_thread():
                heartbeat_thread.join(timeout=2)
            release_lock(job_id, owner)

    def _lease_loop(
        self,
        job_id: str,
        owner: str,
        lease_lost: threading.Event,
        stop: threading.Event,
    ) -> None:
        interval = max(1.0, LEASE_SECONDS / 3)
        while not stop.wait(interval):
            try:
                if not heartbeat(job_id, owner, lease_seconds=LEASE_SECONDS):
                    lease_lost.set()
                    return
            except (OSError, StorageError, ValueError):
                lease_lost.set()
                return

    def _should_stop(
        self,
        job_id: str,
        *,
        lease_lost: threading.Event | None = None,
        terminal_only: bool = False,
    ) -> bool:
        if self._stop.is_set() or (lease_lost and lease_lost.is_set()):
            return True
        control = job_control(job_id)
        if not control:
            return True
        if control["cancel_requested"] or (control["terminal"] and not terminal_only):
            return True
        if control["pause_requested"]:
            while not self._stop.is_set():
                if lease_lost and lease_lost.is_set():
                    return True
                current = job_control(job_id)
                if not current or current["cancel_requested"] or current["terminal"]:
                    return True
                if not current["pause_requested"]:
                    return False
                self._wake.wait(0.5)
                self._wake.clear()
            return True
        return False

    def _wait_retry(self, seconds: float, lease_lost: threading.Event | None = None) -> bool:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            if lease_lost and lease_lost.is_set():
                return False
            self._stop.wait(min(0.5, max(0.01, deadline - time.monotonic())))
        return not self._stop.is_set() and not (lease_lost and lease_lost.is_set())

    def _wait_request_slot(self, seconds: float, job_id: str, lease_lost: threading.Event) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        paused = False
        while True:
            if self._stop.is_set():
                raise _AcquisitionStopped(shutdown=True)
            if lease_lost and lease_lost.is_set():
                raise _AcquisitionStopped
            control = job_control(job_id)
            if not control or control["cancel_requested"] or control["terminal"]:
                raise _AcquisitionStopped
            if control["pause_requested"]:
                paused = True
                self._stop.wait(0.5)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                control = job_control(job_id)
                if not control or control["cancel_requested"] or control["terminal"]:
                    raise _AcquisitionStopped
                if control["pause_requested"]:
                    paused = True
                    continue
                return paused
            self._stop.wait(min(0.5, remaining))

    def _request_guard(self, provider: str, job_id: str, lease_lost: threading.Event):
        """Reserve a durable request slot before each provider HTTP request."""

        def reserve() -> None:
            while True:
                reservation = reserve_provider_request(provider)
                paused = self._wait_request_slot(
                    float(reservation.get("wait_seconds", 0.0)), job_id, lease_lost
                )
                if not paused:
                    return

        return reserve

    def _run_index(
        self,
        job_id: str,
        code: str,
        settings: dict[str, object],
        owner: str,
        lease_lost: threading.Event,
        *,
        blocked_providers: set[str] | None = None,
    ) -> None:
        blocked_providers = blocked_providers if blocked_providers is not None else set()
        if "fred" in blocked_providers:
            record_item(
                job_id,
                "index",
                code,
                status="failed",
                provider="fred",
                error_type="ProviderRateLimited",
                error_message="FRED request budget or quota is unavailable for this job",
                validation_status="failed",
                owner_token=owner,
            )
            return
        attempts = int(settings["retry_attempts"])
        backoff = float(settings["retry_backoff_seconds"])
        last_error: BaseException | None = None
        for attempt in range(attempts):
            set_item_running(job_id, "index", code, owner_token=owner)
            try:
                result = acquire_index_closes(
                    code, request_guard=self._request_guard("fred", job_id, lease_lost)
                )
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
                    owner_token=owner,
                )
                return
            except LeaseLostError:
                raise
            except _AcquisitionStopped as stopped:
                if stopped.shutdown:
                    raise
                return
            except (ProviderRateLimited, ProviderAccessDenied) as exc:
                blocked_providers.add("fred")
                state = (
                    "budget_exhausted"
                    if isinstance(exc, ProviderBudgetExhausted)
                    else "quota_exhausted"
                    if isinstance(exc, ProviderRateLimited)
                    else "auth_failed"
                )
                mark_provider_status(
                    "fred",
                    state,
                    str(exc),
                )
                last_error = exc
                append_provider_error(
                    job_id,
                    {
                        **_safe_error(exc),
                        "provider": "fred",
                        "instrument": code,
                        "quota": isinstance(exc, ProviderRateLimited),
                    },
                    owner_token=owner,
                )
                break
            except IndexHistoryError as exc:
                last_error = exc
                append_provider_error(
                    job_id,
                    {**_safe_error(exc), "provider": "fred", "instrument": code},
                    owner_token=owner,
                )
                break
            except (ProviderUnavailable, StorageError, OSError) as exc:
                last_error = exc
                append_provider_error(
                    job_id,
                    {
                        **_safe_error(exc),
                        "provider": "fred",
                        "instrument": code,
                        "retryable": isinstance(exc, (ProviderUnavailable, OSError)),
                    },
                    owner_token=owner,
                )
                if attempt + 1 < attempts and self._wait_retry(backoff * (2**attempt), lease_lost):
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
            owner_token=owner,
        )

    def _run_equity(
        self,
        job_id: str,
        symbol: str,
        provider: str,
        secondary_provider: object,
        settings: dict[str, object],
        latest_by_provider: dict[str, dict[str, date]],
        expected_session: date | None,
        owner: str,
        lease_lost: threading.Event,
        *,
        preferred_provider: object = None,
        blocked_providers: set[str] | None = None,
    ) -> None:
        blocked_providers = blocked_providers if blocked_providers is not None else set()
        providers: list[str] = []
        if isinstance(preferred_provider, str) and preferred_provider in PROVIDERS:
            providers.append(preferred_provider)
        if provider not in providers:
            providers.append(provider)
        if (
            isinstance(secondary_provider, str)
            and secondary_provider in PROVIDERS
            and secondary_provider not in providers
        ):
            providers.append(secondary_provider)
        providers = [selected for selected in providers if selected not in blocked_providers]
        if not providers:
            record_item(
                job_id,
                "equity",
                symbol,
                status="failed",
                provider=provider,
                error_type="ProviderRateLimited",
                error_message="All configured providers are unavailable for this job",
                validation_status="failed",
                owner_token=owner,
            )
            return
        attempts = int(settings["retry_attempts"])
        backoff = float(settings["retry_backoff_seconds"])
        overlap = int(settings["overlap_days"])
        last_error: BaseException | None = None
        incomplete_result: tuple[str, dict[str, object], int] | None = None
        for selected in providers:
            last_session = latest_by_provider.get(selected, {}).get(symbol)
            start = (
                last_session - timedelta(days=overlap) if last_session and overlap else last_session
            )
            for attempt in range(attempts):
                if self._should_stop(job_id, lease_lost=lease_lost):
                    return
                set_item_running(job_id, "equity", symbol, owner_token=owner)
                try:
                    result = acquire_and_store(
                        symbol,
                        provider=selected,
                        period="5y",
                        start_date=start if selected != "alpha_vantage" else None,
                        request_guard=self._request_guard(selected, job_id, lease_lost),
                    )
                    provider_end = (
                        latest_provider_eligible_session(selected, expected_session)
                        if expected_session
                        else None
                    ) or date.fromisoformat(str(result["last_session"]))
                    missing = len(
                        missing_sessions_for_instrument(
                            symbol,
                            selected,
                            start_date=start,
                            end_date=provider_end,
                        )
                    )
                    if missing == 0:
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
                            validation_status="validated",
                            owner_token=owner,
                        )
                        return
                    incomplete_result = (selected, result, missing)
                    break
                except LeaseLostError:
                    raise
                except _AcquisitionStopped as stopped:
                    if stopped.shutdown:
                        raise
                    return
                except (ProviderRateLimited, ProviderAccessDenied) as exc:
                    last_error = exc
                    blocked_providers.add(selected)
                    state = (
                        "budget_exhausted"
                        if isinstance(exc, ProviderBudgetExhausted)
                        else "quota_exhausted"
                        if isinstance(exc, ProviderRateLimited)
                        else "auth_failed"
                    )
                    mark_provider_status(
                        selected,
                        state,
                        str(exc),
                    )
                    append_provider_error(
                        job_id,
                        {
                            **_safe_error(exc),
                            "instrument": symbol,
                            "provider": selected,
                            "quota": isinstance(exc, ProviderRateLimited),
                        },
                        owner_token=owner,
                    )
                    break
                except (ProviderError, StorageError, OSError, ValueError) as exc:
                    last_error = exc
                    append_provider_error(
                        job_id,
                        {
                            **_safe_error(exc),
                            "instrument": symbol,
                            "provider": selected,
                            "retryable": isinstance(exc, (ProviderUnavailable, OSError)),
                        },
                        owner_token=owner,
                    )
                    if (
                        isinstance(exc, RETRYABLE_ERRORS)
                        and attempt + 1 < attempts
                        and self._wait_retry(backoff * (2**attempt), lease_lost)
                    ):
                        continue
                    break
            if incomplete_result is not None:
                continue
        if incomplete_result is not None:
            selected, result, missing = incomplete_result
            record_item(
                job_id,
                "equity",
                symbol,
                status="incomplete",
                provider=selected,
                last_session=str(result["last_session"]),
                inserted=int(result["inserted"]),
                updated=int(result["updated"]),
                unchanged=int(result["unchanged"]),
                stale=int(result["stale"]),
                error_type=type(last_error).__name__ if last_error else None,
                error_message=str(last_error) if last_error else None,
                validation_status="incomplete",
                missing_sessions=missing,
                owner_token=owner,
            )
            return
        record_item(
            job_id,
            "equity",
            symbol,
            status="failed",
            provider=providers[-1],
            error_type=type(last_error).__name__ if last_error else "ProviderError",
            error_message=str(last_error) if last_error else "Acquisition failed",
            validation_status="failed",
            owner_token=owner,
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
