"""Durable state and control operations for daily acquisition jobs.

This module owns only scheduler metadata. Historical OHLCV and close-only index
observations continue to use the existing transactional import paths.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finintel.storage.db import (
    SCHEMA_VERSION,
    StorageError,
    _database_version,
    _read_connection,
    _write_connection,
    database_path,
    serialized_storage_write,
)

PROVIDERS = ("alpaca_sip", "tiingo", "alpha_vantage")
DEFAULT_PROVIDER_LIMITS = {
    # Conservative defaults; users with larger entitlements can raise these
    # values in local settings without changing provider adapters.
    "alpaca_sip": {"requests_per_minute": 180, "daily_request_budget": 10000},
    "tiingo": {"requests_per_minute": 60, "daily_request_budget": 1000},
    "alpha_vantage": {"requests_per_minute": 5, "daily_request_budget": 25},
}
JOB_STATUSES = ("scheduled", "running", "paused", "completed", "partial", "failed", "cancelled")
ITEM_STATUSES = ("pending", "running", "completed", "incomplete", "failed", "cancelled")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
TERMINAL_STATUSES = ("completed", "partial", "failed", "cancelled")
DEFAULT_SETTINGS = {
    "enabled": False,
    "schedule_time": "20:00",
    "timezone": "America/New_York",
    "primary_provider": "alpaca_sip",
    "secondary_provider": "tiingo",
    "overlap_days": 8,
    "retry_attempts": 3,
    "retry_backoff_seconds": 2.0,
    "catch_up_limit": 5,
    "provider_limits": DEFAULT_PROVIDER_LIMITS,
    "updated_at": None,
}


class LeaseLostError(StorageError):
    """The worker no longer owns the durable acquisition lease."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None, *, precise: bool = False) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds" if precise else "seconds")


def _json(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _provider_limits(value: object) -> dict[str, dict[str, int]]:
    """Normalize stored/provider-supplied request policies without mutation."""
    if not isinstance(value, dict):
        value = {}
    result = {provider: dict(limits) for provider, limits in DEFAULT_PROVIDER_LIMITS.items()}
    for provider, raw in value.items():
        if provider not in PROVIDERS:
            raise ValueError(f"provider_limits contains unsupported provider: {provider}")
        if not isinstance(raw, dict):
            raise TypeError(f"provider_limits.{provider} must be an object")
        result[provider].update(raw)
    for provider, limits in result.items():
        requests_per_minute = limits.get("requests_per_minute")
        daily_request_budget = limits.get("daily_request_budget")
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, int)
            or not 1 <= requests_per_minute <= 10000
        ):
            raise ValueError(f"provider_limits.{provider}.requests_per_minute must be 1-10000")
        if (
            isinstance(daily_request_budget, bool)
            or not isinstance(daily_request_budget, int)
            or not 0 <= daily_request_budget <= 1000000
        ):
            raise ValueError(f"provider_limits.{provider}.daily_request_budget must be 0-1000000")
    return result


def _settings_row(row) -> dict[str, object]:
    if row is None:
        return {**DEFAULT_SETTINGS, "provider_limits": _provider_limits(DEFAULT_PROVIDER_LIMITS)}
    row_columns = row.keys()
    raw_limits = _json(row["provider_limits"] if "provider_limits" in row_columns else None, {})
    return {
        "enabled": bool(row["enabled"]),
        "schedule_time": row["schedule_time"],
        "timezone": row["timezone"],
        "primary_provider": row["primary_provider"],
        "secondary_provider": row["secondary_provider"],
        "overlap_days": int(row["overlap_days"]),
        "retry_attempts": int(row["retry_attempts"]),
        "retry_backoff_seconds": float(row["retry_backoff_seconds"]),
        "catch_up_limit": int(row["catch_up_limit"]),
        "provider_limits": _provider_limits(raw_limits),
        "updated_at": row["updated_at"] or None,
    }


def _validate_settings(values: dict[str, object]) -> dict[str, object]:
    merged = {**DEFAULT_SETTINGS, **values}
    if not isinstance(merged["enabled"], bool):
        raise TypeError("enabled must be a boolean")
    schedule_time = merged["schedule_time"]
    if not isinstance(schedule_time, str) or not TIME_PATTERN.fullmatch(schedule_time):
        raise ValueError("schedule_time must use HH:MM format")
    timezone = merged["timezone"]
    if not isinstance(timezone, str) or not timezone or len(timezone) > 80:
        raise ValueError("timezone is invalid")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone is not available on this system") from exc
    for field in ("primary_provider", "secondary_provider"):
        provider = merged[field]
        if provider is not None and provider not in PROVIDERS:
            raise ValueError(f"{field} is not supported")
    if merged["secondary_provider"] == merged["primary_provider"]:
        merged["secondary_provider"] = None
    if not isinstance(merged["overlap_days"], int) or not 0 <= merged["overlap_days"] <= 60:
        raise ValueError("overlap_days must be between 0 and 60")
    if not isinstance(merged["retry_attempts"], int) or not 1 <= merged["retry_attempts"] <= 5:
        raise ValueError("retry_attempts must be between 1 and 5")
    if (
        isinstance(merged["retry_backoff_seconds"], bool)
        or not isinstance(merged["retry_backoff_seconds"], (int, float))
        or not 0 <= float(merged["retry_backoff_seconds"]) <= 300
    ):
        raise ValueError("retry_backoff_seconds must be between 0 and 300")
    if not isinstance(merged["catch_up_limit"], int) or not 1 <= merged["catch_up_limit"] <= 30:
        raise ValueError("catch_up_limit must be between 1 and 30")
    merged["provider_limits"] = _provider_limits(merged["provider_limits"])
    return merged


def get_settings(*, db: Path | None = None) -> dict[str, object]:
    path = db if db is not None else database_path()
    if path.is_file() and _database_version(path) < SCHEMA_VERSION:
        migration_conn = _write_connection(path)
        migration_conn.close()
    conn = _read_connection(path)
    if conn is None:
        return {**DEFAULT_SETTINGS, "provider_limits": _provider_limits(DEFAULT_PROVIDER_LIMITS)}
    try:
        row = conn.execute("SELECT * FROM daily_acquisition_settings WHERE id=1").fetchone()
        return _settings_row(row)
    except Exception as exc:
        raise StorageError("Could not read daily acquisition settings") from exc
    finally:
        conn.close()


@serialized_storage_write
def update_settings(values: dict[str, object], *, db: Path | None = None) -> dict[str, object]:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        row = conn.execute("SELECT * FROM daily_acquisition_settings WHERE id=1").fetchone()
        current = _settings_row(row)
        requested = dict(values)
        if isinstance(requested.get("provider_limits"), dict):
            requested_limits = {
                provider: dict(limits) for provider, limits in current["provider_limits"].items()
            }
            for provider, limits in requested["provider_limits"].items():
                if isinstance(limits, dict):
                    requested_limits.setdefault(provider, {}).update(limits)
            requested["provider_limits"] = requested_limits
        merged = _validate_settings({**current, **requested})
        updated_at = _iso(_now())
        with conn:
            conn.execute(
                "UPDATE daily_acquisition_settings SET enabled=?, schedule_time=?, timezone=?, "
                "primary_provider=?, secondary_provider=?, overlap_days=?, retry_attempts=?, "
                "retry_backoff_seconds=?, catch_up_limit=?, provider_limits=?, updated_at=? WHERE id=1",
                (
                    int(merged["enabled"]),
                    merged["schedule_time"],
                    merged["timezone"],
                    merged["primary_provider"],
                    merged["secondary_provider"],
                    merged["overlap_days"],
                    merged["retry_attempts"],
                    float(merged["retry_backoff_seconds"]),
                    merged["catch_up_limit"],
                    json.dumps(merged["provider_limits"], sort_keys=True, separators=(",", ":")),
                    updated_at,
                ),
            )
        merged["updated_at"] = updated_at
        return merged
    except Exception as exc:
        raise StorageError("Could not update daily acquisition settings") from exc
    finally:
        conn.close()


def _provider_policy(provider: str, settings: dict[str, object]) -> dict[str, int]:
    limits = settings.get("provider_limits")
    if isinstance(limits, dict) and isinstance(limits.get(provider), dict):
        return limits[provider]
    if provider in DEFAULT_PROVIDER_LIMITS:
        return DEFAULT_PROVIDER_LIMITS[provider]
    # FRED is used for the five index series. It has no user API quota in this
    # application, but it still gets a modest cross-process pacing interval.
    return {"requests_per_minute": 30, "daily_request_budget": 0}


@serialized_storage_write
def reserve_provider_request(
    provider: str, *, db: Path | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Atomically reserve one provider request slot across backend processes.

    The returned ``wait_seconds`` is safe to sleep before making the request.
    A reservation is counted even if the process later crashes, which is
    conservative for provider quotas and avoids duplicate request bursts after
    restart. A zero daily budget means unlimited for that provider.
    """
    from finintel.market_data.provider import ProviderBudgetExhausted, ProviderRateLimited

    path = db if db is not None else database_path()
    settings = get_settings(db=path)
    policy = _provider_policy(provider, settings)
    current = (now or _now()).astimezone(UTC)
    usage_date = current.date().isoformat()
    interval_microseconds = math.ceil(60_000_000 / int(policy["requests_per_minute"]))
    conn = _write_connection(path)
    try:
        # A deferred transaction can allow two processes to read the same slot
        # before either attempts its first write. BEGIN IMMEDIATE serializes
        # reservations across processes before the read.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM daily_acquisition_provider_usage WHERE provider=? AND usage_date=?",
            (provider, usage_date),
        ).fetchone()
        request_count = int(row["request_count"]) if row else 0
        budget = int(policy["daily_request_budget"])
        current_state = row["quota_state"] if row else "available"
        if current_state == "auth_failed":
            from finintel.market_data.provider import ProviderAccessDenied

            conn.rollback()
            raise ProviderAccessDenied(row["quota_message"] or f"{provider} authentication failed")
        if current_state == "quota_exhausted":
            conn.rollback()
            raise ProviderRateLimited(row["quota_message"] or f"{provider} quota exhausted")
        if budget and request_count >= budget:
            message = f"{provider} local daily request budget exhausted"
            if row:
                conn.execute(
                    "UPDATE daily_acquisition_provider_usage SET quota_state='budget_exhausted', "
                    "quota_message=?, updated_at=? WHERE provider=? AND usage_date=?",
                    (message, _iso(current), provider, usage_date),
                )
            else:
                conn.execute(
                    "INSERT INTO daily_acquisition_provider_usage "
                    "(provider,usage_date,request_count,quota_state,quota_message,updated_at) "
                    "VALUES (?, ?, 0, 'budget_exhausted', ?, ?)",
                    (provider, usage_date, message, _iso(current)),
                )
            # Commit the durable exhaustion marker before raising. The caller
            # may restart immediately and must not receive another slot.
            conn.commit()
            raise ProviderBudgetExhausted(message)

        previous = None
        if row and row["last_request_at"]:
            try:
                previous = datetime.fromisoformat(row["last_request_at"])
            except ValueError:
                previous = None
        interval = timedelta(microseconds=interval_microseconds)
        slot = max(current, (previous + interval) if previous else current)
        wait_seconds = max(0.0, (slot - current).total_seconds())
        new_count = request_count + 1
        state = "throttled" if wait_seconds > 0 else "available"
        conn.execute(
            "INSERT INTO daily_acquisition_provider_usage "
            "(provider,usage_date,request_count,last_request_at,quota_state,quota_message,updated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(provider,usage_date) DO UPDATE SET request_count=excluded.request_count, "
            "last_request_at=excluded.last_request_at, quota_state=excluded.quota_state, "
            "quota_message=NULL, updated_at=excluded.updated_at",
            (provider, usage_date, new_count, _iso(slot, precise=True), state, _iso(current)),
        )
        conn.commit()
        return {
            "provider": provider,
            "usage_date": usage_date,
            "request_count": new_count,
            "daily_request_budget": budget,
            "wait_seconds": wait_seconds,
            "quota_state": state,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def provider_request_guard(provider: str, *, db: Path | None = None):
    """Create the shared request hook used by scheduled and manual acquisition."""

    def reserve() -> None:
        reservation = reserve_provider_request(provider, db=db)
        wait_seconds = float(reservation.get("wait_seconds", 0.0))
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return reserve


@serialized_storage_write
def mark_provider_status(
    provider: str,
    state: str,
    message: str | None = None,
    *,
    db: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Persist safe quota/auth state for monitoring and restart recovery."""
    if state not in {
        "available",
        "throttled",
        "budget_exhausted",
        "quota_exhausted",
        "auth_failed",
    }:
        raise ValueError("Invalid provider quota state")
    path = db if db is not None else database_path()
    current = (now or _now()).astimezone(UTC)
    usage_date = current.date().isoformat()
    conn = _write_connection(path)
    try:
        with conn:
            existing = conn.execute(
                "SELECT quota_state FROM daily_acquisition_provider_usage "
                "WHERE provider=? AND usage_date=?",
                (provider, usage_date),
            ).fetchone()
            if (
                existing
                and existing["quota_state"] == "budget_exhausted"
                and state != "budget_exhausted"
            ):
                return
            if (
                state == "available"
                and existing
                and existing["quota_state"] in {"quota_exhausted", "auth_failed"}
            ):
                return
            conn.execute(
                "INSERT INTO daily_acquisition_provider_usage "
                "(provider,usage_date,request_count,quota_state,quota_message,updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?) "
                "ON CONFLICT(provider,usage_date) DO UPDATE SET quota_state=excluded.quota_state, "
                "quota_message=excluded.quota_message, updated_at=excluded.updated_at",
                (provider, usage_date, state, (message or "")[:240] or None, _iso(current)),
            )
    finally:
        conn.close()


@serialized_storage_write
def reset_provider_status(
    provider: str, *, db: Path | None = None, now: datetime | None = None
) -> None:
    """Clear a persisted provider failure after an explicit user action."""
    path = db if db is not None else database_path()
    current = (now or _now()).astimezone(UTC)
    conn = _write_connection(path)
    try:
        with conn:
            conn.execute(
                "UPDATE daily_acquisition_provider_usage SET quota_state='available', "
                "quota_message=NULL, updated_at=? WHERE provider=? AND usage_date=?",
                (_iso(current), provider, current.date().isoformat()),
            )
    finally:
        conn.close()


def provider_usage_status(
    *, db: Path | None = None, now: datetime | None = None
) -> dict[str, dict[str, object]]:
    """Return current-day request usage without exposing provider credentials."""
    path = db if db is not None else database_path()
    settings = get_settings(db=path)
    current = (now or _now()).astimezone(UTC)
    usage_date = current.date().isoformat()
    conn = _read_connection(path)
    rows = (
        []
        if conn is None
        else conn.execute(
            "SELECT * FROM daily_acquisition_provider_usage WHERE usage_date=?",
            (usage_date,),
        ).fetchall()
    )
    try:
        by_provider = {row["provider"]: row for row in rows}
        result: dict[str, dict[str, object]] = {}
        for provider in (*PROVIDERS, "fred"):
            policy = _provider_policy(provider, settings)
            row = by_provider.get(provider)
            result[provider] = {
                "requests_used": int(row["request_count"]) if row else 0,
                "daily_request_budget": int(policy["daily_request_budget"]),
                "requests_per_minute": int(policy["requests_per_minute"]),
                "quota_state": row["quota_state"] if row else "available",
                "quota_message": row["quota_message"] if row else None,
                "last_request_at": row["last_request_at"] if row else None,
                "usage_date": usage_date,
            }
        return result
    finally:
        if conn is not None:
            conn.close()


def latest_successful_trading_session(*, db: Path | None = None) -> str | None:
    """Return the latest session persisted by a completed acquisition job."""
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(i.last_session) AS last_session "
            "FROM daily_acquisition_items i "
            "JOIN daily_acquisition_jobs j ON j.id=i.job_id "
            "WHERE j.status='completed' AND i.status='completed' AND i.last_session IS NOT NULL"
        ).fetchone()
        return row["last_session"] if row else None
    finally:
        conn.close()


def _job_row(row) -> dict[str, object] | None:
    if row is None:
        return None
    started = row["started_at"]
    finished = row["finished_at"]
    duration_ms = None
    if started:
        try:
            duration_ms = max(
                0,
                int(
                    (
                        datetime.fromisoformat(finished or _iso(_now()))
                        - datetime.fromisoformat(started)
                    ).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            duration_ms = None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "provider": row["provider"],
        "secondary_provider": row["secondary_provider"],
        "schedule_date": row["schedule_date"],
        "scheduled_for": row["scheduled_for"],
        "started_at": started,
        "finished_at": finished,
        "last_heartbeat": row["last_heartbeat"],
        "current_instrument": row["current_instrument"],
        "total_instruments": int(row["total_instruments"]),
        "completed_instruments": int(row["completed_instruments"]),
        "failed_instruments": int(row["failed_instruments"]),
        "incomplete_instruments": int(row["incomplete_instruments"]),
        "pending_instruments": int(row["pending_instruments"]),
        "successful_imports": int(row["successful_imports"]),
        "inserted": int(row["inserted"]),
        "updated": int(row["updated"]),
        "unchanged": int(row["unchanged"]),
        "retry_count": int(row["retry_count"]),
        "provider_errors": _json(row["provider_errors"], []),
        "index_results": _json(row["index_results"], {}),
        "last_error": row["last_error"],
        "cancel_requested": bool(row["cancel_requested"]),
        "pause_requested": bool(row["pause_requested"]),
        "duration_ms": duration_ms,
    }


def _fetch_job(conn, job_id: str):
    return conn.execute("SELECT * FROM daily_acquisition_jobs WHERE id=?", (job_id,)).fetchone()


def get_job(
    job_id: str, *, include_items: bool = True, db: Path | None = None
) -> dict[str, object] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        job = _job_row(_fetch_job(conn, job_id))
        if job is None:
            return None
        if include_items:
            rows = conn.execute(
                "SELECT kind, code, provider, status, attempts, started_at, finished_at, "
                "last_session, inserted, updated, unchanged, stale, error_type, error_message, "
                "validation_status, missing_sessions FROM daily_acquisition_items "
                "WHERE job_id=? ORDER BY kind, code",
                (job_id,),
            ).fetchall()
            job["items"] = [dict(row) for row in rows]
        return job
    except Exception as exc:
        raise StorageError("Could not read daily acquisition job") from exc
    finally:
        conn.close()


def get_latest_job(*, db: Path | None = None) -> dict[str, object] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM daily_acquisition_jobs ORDER BY scheduled_for DESC, id DESC LIMIT 1"
        ).fetchone()
        return _job_row(row)
    except Exception as exc:
        raise StorageError("Could not read daily acquisition history") from exc
    finally:
        conn.close()


def get_active_job(*, db: Path | None = None) -> dict[str, object] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM daily_acquisition_jobs WHERE status IN ('scheduled','running','paused') "
            "ORDER BY scheduled_for ASC, id ASC LIMIT 1"
        ).fetchone()
        return _job_row(row)
    except Exception as exc:
        raise StorageError("Could not read active daily acquisition job") from exc
    finally:
        conn.close()


def get_automatic_job(schedule_date: str, *, db: Path | None = None) -> dict[str, object] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM daily_acquisition_jobs WHERE kind='automatic' AND schedule_date=?",
            (schedule_date,),
        ).fetchone()
        return _job_row(row)
    finally:
        conn.close()


def list_automatic_jobs(
    *, through: str | None = None, db: Path | None = None
) -> list[dict[str, object]]:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return []
    try:
        if through is None:
            rows = conn.execute(
                "SELECT * FROM daily_acquisition_jobs WHERE kind='automatic' "
                "ORDER BY schedule_date ASC, id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM daily_acquisition_jobs WHERE kind='automatic' AND schedule_date<=? "
                "ORDER BY schedule_date ASC, id ASC",
                (through,),
            ).fetchall()
        return [_job_row(row) for row in rows]
    finally:
        conn.close()


def latest_completed_automatic_session(*, db: Path | None = None) -> str | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(schedule_date) AS schedule_date FROM daily_acquisition_jobs "
            "WHERE kind='automatic' AND status='completed' AND schedule_date IS NOT NULL"
        ).fetchone()
        return row["schedule_date"] if row else None
    finally:
        conn.close()


@serialized_storage_write
def create_job(
    *,
    kind: str,
    provider: str,
    secondary_provider: str | None,
    scheduled_for: str,
    schedule_date: str | None,
    equity_symbols: list[str],
    index_codes: list[str],
    db: Path | None = None,
) -> dict[str, object]:
    if kind not in ("automatic", "manual", "retry_failed") or provider not in PROVIDERS:
        raise ValueError("Invalid daily acquisition job")
    if secondary_provider == provider:
        secondary_provider = None
    if secondary_provider is not None and secondary_provider not in PROVIDERS:
        raise ValueError("Invalid secondary acquisition provider")
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    job_id = uuid.uuid4().hex
    codes = [("equity", symbol, provider) for symbol in equity_symbols] + [
        ("index", code, "fred") for code in index_codes
    ]
    try:
        if kind == "automatic" and schedule_date is not None:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM daily_acquisition_jobs WHERE kind='automatic' AND schedule_date=?",
                (schedule_date,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return _job_row(existing) or {"id": existing["id"]}
        with conn:
            conn.execute(
                "INSERT INTO daily_acquisition_jobs "
                "(id,kind,status,provider,secondary_provider,schedule_date,scheduled_for,"
                "total_instruments,pending_instruments) VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    provider,
                    secondary_provider,
                    schedule_date,
                    scheduled_for,
                    len(codes),
                    len(codes),
                ),
            )
            conn.executemany(
                "INSERT INTO daily_acquisition_items(job_id,kind,code,provider,status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (
                    (job_id, item_kind, code, item_provider)
                    for item_kind, code, item_provider in codes
                ),
            )
        return get_job(job_id, db=path) or {"id": job_id}
    except Exception as exc:
        raise StorageError("Could not create daily acquisition job") from exc
    finally:
        conn.close()


@serialized_storage_write
def merge_job_items(
    job_id: str,
    *,
    provider: str,
    equity_symbols: list[str],
    index_codes: list[str],
    db: Path | None = None,
) -> dict[str, object] | None:
    """Add only newly discovered instruments to an existing durable job."""
    if provider not in PROVIDERS:
        raise ValueError("Invalid acquisition provider")
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            job = _fetch_job(conn, job_id)
            if job is None:
                return None
            existing = {
                (row["kind"], row["code"])
                for row in conn.execute(
                    "SELECT kind,code FROM daily_acquisition_items WHERE job_id=?", (job_id,)
                )
            }
            candidates = [("equity", symbol, provider) for symbol in equity_symbols] + [
                ("index", code, "fred") for code in index_codes
            ]
            additions = []
            candidate_keys = set(existing)
            for item in candidates:
                if item[:2] not in candidate_keys:
                    additions.append(item)
                    candidate_keys.add(item[:2])
            if additions:
                conn.executemany(
                    "INSERT INTO daily_acquisition_items(job_id,kind,code,provider,status) "
                    "VALUES (?, ?, ?, ?, 'pending')",
                    (
                        (job_id, kind, code, item_provider)
                        for kind, code, item_provider in additions
                    ),
                )
                conn.execute(
                    "UPDATE daily_acquisition_jobs SET status=CASE WHEN status IN "
                    "('completed','partial','failed') THEN 'scheduled' ELSE status END, "
                    "finished_at=NULL, cancel_requested=0, last_error=NULL WHERE id=?",
                    (job_id,),
                )
            _recalculate(conn, job_id)
        return get_job(job_id, db=path)
    finally:
        conn.close()


@serialized_storage_write
def requeue_job(job_id: str, *, db: Path | None = None) -> dict[str, object] | None:
    """Requeue failed or incomplete work while preserving completed items."""
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            job = _fetch_job(conn, job_id)
            if job is None or job["status"] == "cancelled":
                return _job_row(job)
            conn.execute(
                "UPDATE daily_acquisition_items SET status='pending', finished_at=NULL, "
                "error_type=NULL, error_message=NULL, validation_status='pending' "
                "WHERE job_id=? AND status IN ('failed','incomplete','running')",
                (job_id,),
            )
            conn.execute(
                "UPDATE daily_acquisition_jobs SET status='scheduled', finished_at=NULL, "
                "cancel_requested=0, pause_requested=0, last_error=NULL WHERE id=?",
                (job_id,),
            )
            _recalculate(conn, job_id)
        return get_job(job_id, db=path)
    finally:
        conn.close()


@serialized_storage_write
def claim_job(
    job_id: str, owner_token: str, *, lease_seconds: int = 120, db: Path | None = None
) -> bool:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    now = _now()
    now_text = _iso(now)
    expires = _iso(now + timedelta(seconds=lease_seconds))
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = _fetch_job(conn, job_id)
        if job is None or job["status"] in ("completed", "partial", "failed", "cancelled"):
            conn.rollback()
            return False
        lock = conn.execute("SELECT * FROM daily_acquisition_lock WHERE id=1").fetchone()
        if lock and lock["owner_token"] != owner_token and lock["expires_at"] > now_text:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO daily_acquisition_lock(id,owner_token,job_id,acquired_at,heartbeat_at,expires_at) "
            "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET owner_token=excluded.owner_token, "
            "job_id=excluded.job_id, acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, "
            "expires_at=excluded.expires_at",
            (owner_token, job_id, now_text, now_text, expires),
        )
        conn.execute(
            "UPDATE daily_acquisition_jobs SET status='running', started_at=COALESCE(started_at, ?), "
            "last_heartbeat=?, pause_requested=0 WHERE id=?",
            (now_text, now_text, job_id),
        )
        conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _assert_lease(conn, job_id: str, owner_token: str) -> None:
    now_text = _iso(_now())
    row = conn.execute(
        "SELECT l.owner_token, l.expires_at, j.status, j.cancel_requested, j.pause_requested "
        "FROM daily_acquisition_lock l JOIN daily_acquisition_jobs j ON j.id=? "
        "WHERE l.id=1 AND l.job_id=?",
        (job_id, job_id),
    ).fetchone()
    if (
        row is None
        or row["owner_token"] != owner_token
        or row["expires_at"] <= now_text
        or row["status"] in TERMINAL_STATUSES
        or row["cancel_requested"]
    ):
        raise LeaseLostError("Daily acquisition worker lease is no longer owned")


@serialized_storage_write
def heartbeat(
    job_id: str, owner_token: str, *, lease_seconds: int = 120, db: Path | None = None
) -> bool:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    now = _now()
    now_text = _iso(now)
    try:
        cursor = conn.execute(
            "UPDATE daily_acquisition_lock SET heartbeat_at=?, expires_at=? "
            "WHERE id=1 AND owner_token=? AND job_id=? AND expires_at>? "
            "AND EXISTS (SELECT 1 FROM daily_acquisition_jobs WHERE id=? "
            "AND status NOT IN ('completed','partial','failed','cancelled') "
            "AND cancel_requested=0)",
            (
                now_text,
                _iso(now + timedelta(seconds=lease_seconds)),
                owner_token,
                job_id,
                now_text,
                job_id,
            ),
        )
        if cursor.rowcount == 1:
            conn.execute(
                "UPDATE daily_acquisition_jobs SET last_heartbeat=? WHERE id=?",
                (now_text, job_id),
            )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


@serialized_storage_write
def set_current_instrument(
    job_id: str,
    code: str | None,
    *,
    owner_token: str | None = None,
    db: Path | None = None,
) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            if owner_token is not None:
                _assert_lease(conn, job_id, owner_token)
            conn.execute(
                "UPDATE daily_acquisition_jobs SET current_instrument=?, last_heartbeat=? WHERE id=?",
                (code, _iso(_now()), job_id),
            )
    finally:
        conn.close()


@serialized_storage_write
def release_lock(job_id: str, owner_token: str, *, db: Path | None = None) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            conn.execute(
                "DELETE FROM daily_acquisition_lock WHERE id=1 AND owner_token=? AND job_id=?",
                (owner_token, job_id),
            )
    finally:
        conn.close()


def job_control(job_id: str, *, db: Path | None = None) -> dict[str, bool] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT status,cancel_requested,pause_requested FROM daily_acquisition_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "cancel_requested": bool(row["cancel_requested"]),
            "pause_requested": bool(row["pause_requested"]),
            "terminal": row["status"] in ("completed", "partial", "failed", "cancelled"),
        }
    finally:
        conn.close()


@serialized_storage_write
def set_item_running(
    job_id: str,
    kind: str,
    code: str,
    *,
    owner_token: str | None = None,
    db: Path | None = None,
) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            if owner_token is not None:
                _assert_lease(conn, job_id, owner_token)
            conn.execute(
                "UPDATE daily_acquisition_items SET status='running', attempts=attempts+1, "
                "started_at=?, validation_status='pending' WHERE job_id=? AND kind=? AND code=?",
                (_iso(_now()), job_id, kind, code),
            )
    finally:
        conn.close()


@serialized_storage_write
def record_item(
    job_id: str,
    kind: str,
    code: str,
    *,
    status: str,
    provider: str | None = None,
    last_session: str | None = None,
    inserted: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    stale: int = 0,
    error_type: str | None = None,
    error_message: str | None = None,
    validation_status: str = "validated",
    missing_sessions: int = 0,
    index_result: dict[str, object] | None = None,
    owner_token: str | None = None,
    db: Path | None = None,
) -> None:
    if status not in ITEM_STATUSES:
        raise ValueError("Invalid daily acquisition item status")
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            if owner_token is not None:
                _assert_lease(conn, job_id, owner_token)
            conn.execute(
                "UPDATE daily_acquisition_items SET status=?, provider=COALESCE(?, provider), "
                "finished_at=?, last_session=?, inserted=?, updated=?, unchanged=?, stale=?, "
                "error_type=?, error_message=?, validation_status=?, missing_sessions=? "
                "WHERE job_id=? AND kind=? AND code=?",
                (
                    status,
                    provider,
                    _iso(_now()),
                    last_session,
                    inserted,
                    updated,
                    unchanged,
                    stale,
                    error_type,
                    (error_message or "")[:240] or None,
                    validation_status,
                    max(0, missing_sessions),
                    job_id,
                    kind,
                    code,
                ),
            )
            if index_result is not None:
                conn.execute(
                    "UPDATE daily_acquisition_jobs SET index_results=json_set(index_results, ?, json(?)) "
                    "WHERE id=?",
                    ("$." + code, json.dumps(index_result, separators=(",", ":")), job_id),
                )
            _recalculate(conn, job_id)
    finally:
        conn.close()


def _recalculate(conn, job_id: str) -> None:
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status='completed') AS completed, "
        "SUM(status='failed') AS failed, SUM(status='incomplete') AS incomplete, "
        "SUM(status IN ('pending','running')) AS pending, "
        "SUM(status='completed' AND (inserted + updated + unchanged) > 0) AS imports, "
        "COALESCE(SUM(inserted),0) AS inserted, COALESCE(SUM(updated),0) AS updated, "
        "COALESCE(SUM(unchanged),0) AS unchanged, "
        "COALESCE(SUM(CASE WHEN attempts > 1 THEN attempts - 1 ELSE 0 END),0) AS retries "
        "FROM daily_acquisition_items WHERE job_id=?",
        (job_id,),
    ).fetchone()
    conn.execute(
        "UPDATE daily_acquisition_jobs SET total_instruments=?, completed_instruments=?, "
        "failed_instruments=?, pending_instruments=?, successful_imports=?, inserted=?, updated=?, "
        "incomplete_instruments=?, unchanged=?, retry_count=? WHERE id=?",
        (
            int(row["total"] or 0),
            int(row["completed"] or 0),
            int(row["failed"] or 0),
            int(row["pending"] or 0),
            int(row["imports"] or 0),
            int(row["inserted"] or 0),
            int(row["updated"] or 0),
            int(row["incomplete"] or 0),
            int(row["unchanged"] or 0),
            int(row["retries"] or 0),
            job_id,
        ),
    )


@serialized_storage_write
def append_provider_error(
    job_id: str,
    error: dict[str, object],
    *,
    owner_token: str | None = None,
    db: Path | None = None,
) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            if owner_token is not None:
                _assert_lease(conn, job_id, owner_token)
            row = _fetch_job(conn, job_id)
            errors = _json(row["provider_errors"] if row else "[]", [])
            if not isinstance(errors, list):
                errors = []
            safe: dict[str, object] = {
                "code": str(error.get("code", "provider_error"))[:64],
                "message": str(error.get("message", ""))[:240],
            }
            for field in ("provider", "instrument"):
                value = error.get(field)
                if isinstance(value, str) and value:
                    safe[field] = value[:64]
            for field in ("retryable", "quota"):
                value = error.get(field)
                if isinstance(value, bool):
                    safe[field] = value
            safe["recorded_at"] = _iso(_now())
            errors.append(safe)
            conn.execute(
                "UPDATE daily_acquisition_jobs SET provider_errors=?, last_error=? WHERE id=?",
                (json.dumps(errors[-50:], separators=(",", ":")), safe["message"] or None, job_id),
            )
    finally:
        conn.close()


@serialized_storage_write
def finish_job(
    job_id: str,
    *,
    status: str | None = None,
    owner_token: str | None = None,
    db: Path | None = None,
) -> dict[str, object] | None:
    if status is not None and status not in JOB_STATUSES:
        raise ValueError("Invalid daily acquisition status")
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            row = _fetch_job(conn, job_id)
            if row is None:
                return None
            if owner_token is not None:
                _assert_lease(conn, job_id, owner_token)
            _recalculate(conn, job_id)
            row = _fetch_job(conn, job_id)
            if status is None:
                if row["cancel_requested"]:
                    status = "cancelled"
                elif row["failed_instruments"] or row["incomplete_instruments"]:
                    status = "partial"
                elif row["pending_instruments"]:
                    status = "failed"
                else:
                    status = "completed"
            conn.execute(
                "UPDATE daily_acquisition_jobs SET status=?, finished_at=?, current_instrument=NULL, "
                "pause_requested=0 WHERE id=?",
                (status, _iso(_now()), job_id),
            )
        return get_job(job_id, db=path)
    finally:
        conn.close()


@serialized_storage_write
def set_job_pause(job_id: str, paused: bool, *, db: Path | None = None) -> dict[str, object] | None:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            row = _fetch_job(conn, job_id)
            if row is None:
                return None
            if row["status"] in ("completed", "partial", "failed", "cancelled"):
                return _job_row(row)
            conn.execute(
                "UPDATE daily_acquisition_jobs SET pause_requested=?, status=? WHERE id=?",
                (int(paused), "paused" if paused else "running", job_id),
            )
        return get_job(job_id, db=path)
    finally:
        conn.close()


@serialized_storage_write
def cancel_job(job_id: str, *, db: Path | None = None) -> dict[str, object] | None:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        with conn:
            row = _fetch_job(conn, job_id)
            if row is None:
                return None
            if row["status"] not in ("completed", "partial", "failed", "cancelled"):
                conn.execute(
                    "UPDATE daily_acquisition_jobs SET cancel_requested=1, status='cancelled' WHERE id=?",
                    (job_id,),
                )
                conn.execute(
                    "UPDATE daily_acquisition_items SET status='cancelled', finished_at=?, "
                    "validation_status='cancelled' WHERE job_id=? AND status IN ('pending','running')",
                    (_iso(_now()), job_id),
                )
                _recalculate(conn, job_id)
        return get_job(job_id, db=path)
    finally:
        conn.close()


@serialized_storage_write
def retry_failed_job(job_id: str, *, db: Path | None = None) -> dict[str, object] | None:
    path = db if db is not None else database_path()
    conn = _write_connection(path)
    try:
        original = _fetch_job(conn, job_id)
        if original is None:
            return None
        rows = conn.execute(
            "SELECT kind,code,provider FROM daily_acquisition_items "
            "WHERE job_id=? AND status IN ('failed','incomplete')",
            (job_id,),
        ).fetchall()
        if not rows:
            return _job_row(original)
        new_id = uuid.uuid4().hex
        now = _iso(_now())
        equities = [(row["code"], row["provider"]) for row in rows if row["kind"] == "equity"]
        indexes = [row["code"] for row in rows if row["kind"] == "index"]
        with conn:
            conn.execute(
                "INSERT INTO daily_acquisition_jobs(id,kind,status,provider,secondary_provider,scheduled_for,"
                "total_instruments,pending_instruments) VALUES (?, 'retry_failed', 'scheduled', ?, ?, ?, ?, ?)",
                (
                    new_id,
                    original["provider"],
                    original["secondary_provider"],
                    now,
                    len(rows),
                    len(rows),
                ),
            )
            conn.executemany(
                "INSERT INTO daily_acquisition_items(job_id,kind,code,provider,status) VALUES (?, ?, ?, ?, 'pending')",
                ((new_id, "equity", code, provider) for code, provider in equities),
            )
            conn.executemany(
                "INSERT INTO daily_acquisition_items(job_id,kind,code,provider,status) VALUES (?, 'index', ?, 'fred', 'pending')",
                ((new_id, code) for code in indexes),
            )
        return get_job(new_id, db=path)
    except Exception as exc:
        raise StorageError("Could not create a retry job") from exc
    finally:
        conn.close()


def list_history(
    *,
    query: str = "",
    provider: str = "",
    status: str = "",
    kind: str = "",
    execution_date: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Path | None = None,
) -> dict[str, object]:
    if (
        len(query) > 80
        or len(provider) > 40
        or len(status) > 20
        or len(kind) > 20
        or len(execution_date) > 10
    ):
        raise ValueError("Invalid daily acquisition history filters")
    if execution_date:
        try:
            date.fromisoformat(execution_date)
        except ValueError as exc:
            raise ValueError("execution_date must use YYYY-MM-DD format") from exc
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid daily acquisition history pagination")
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    try:
        terms = ["1=1"]
        params: list[object] = []
        if query.strip():
            pattern = "%" + query.strip().replace("%", "\\%").replace("_", "\\_") + "%"
            terms.append(
                "(j.id LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM daily_acquisition_items i WHERE i.job_id=j.id AND i.code LIKE ? ESCAPE '\\'))"
            )
            params.extend([pattern, pattern])
        if provider:
            terms.append(
                "(j.provider=? OR EXISTS (SELECT 1 FROM daily_acquisition_items pi "
                "WHERE pi.job_id=j.id AND pi.provider=?))"
            )
            params.extend([provider, provider])
        if status:
            terms.append("j.status=?")
            params.append(status)
        if kind:
            terms.append("j.kind=?")
            params.append(kind)
        if execution_date:
            terms.append("substr(COALESCE(j.started_at,j.scheduled_for),1,10)=?")
            params.append(execution_date)
        where = " WHERE " + " AND ".join(terms)
        total = conn.execute(
            "SELECT COUNT(*) FROM daily_acquisition_jobs j" + where, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT j.* FROM daily_acquisition_jobs j"
            + where
            + " ORDER BY j.scheduled_for DESC, j.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "items": [_job_row(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        raise StorageError("Could not search daily acquisition history") from exc
    finally:
        conn.close()


def resumable_job(*, db: Path | None = None) -> dict[str, object] | None:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM daily_acquisition_jobs WHERE status IN ('scheduled','running') "
            "ORDER BY scheduled_for ASC, id ASC LIMIT 1"
        ).fetchone()
        return _job_row(row)
    finally:
        conn.close()
