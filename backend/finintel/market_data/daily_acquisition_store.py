"""Durable state and control operations for daily acquisition jobs.

This module owns only scheduler metadata. Historical OHLCV and close-only index
observations continue to use the existing transactional import paths.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finintel.storage.db import (
    StorageError,
    _read_connection,
    _write_connection,
    database_path,
    serialized_storage_write,
)

PROVIDERS = ("alpaca_sip", "tiingo", "alpha_vantage")
JOB_STATUSES = ("scheduled", "running", "paused", "completed", "partial", "failed", "cancelled")
ITEM_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SETTINGS = {
    "enabled": False,
    "schedule_time": "20:00",
    "timezone": "America/New_York",
    "primary_provider": "alpaca_sip",
    "secondary_provider": "tiingo",
    "overlap_days": 8,
    "retry_attempts": 3,
    "retry_backoff_seconds": 2.0,
    "updated_at": None,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _json(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _settings_row(row) -> dict[str, object]:
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {
        "enabled": bool(row["enabled"]),
        "schedule_time": row["schedule_time"],
        "timezone": row["timezone"],
        "primary_provider": row["primary_provider"],
        "secondary_provider": row["secondary_provider"],
        "overlap_days": int(row["overlap_days"]),
        "retry_attempts": int(row["retry_attempts"]),
        "retry_backoff_seconds": float(row["retry_backoff_seconds"]),
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
    return merged


def get_settings(*, db: Path | None = None) -> dict[str, object]:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        return dict(DEFAULT_SETTINGS)
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
    merged = _validate_settings({**get_settings(db=path), **values})
    conn = _write_connection(path)
    try:
        updated_at = _iso(_now())
        with conn:
            conn.execute(
                "UPDATE daily_acquisition_settings SET enabled=?, schedule_time=?, timezone=?, "
                "primary_provider=?, secondary_provider=?, overlap_days=?, retry_attempts=?, "
                "retry_backoff_seconds=?, updated_at=? WHERE id=1",
                (
                    int(merged["enabled"]),
                    merged["schedule_time"],
                    merged["timezone"],
                    merged["primary_provider"],
                    merged["secondary_provider"],
                    merged["overlap_days"],
                    merged["retry_attempts"],
                    float(merged["retry_backoff_seconds"]),
                    updated_at,
                ),
            )
        merged["updated_at"] = updated_at
        return merged
    except Exception as exc:
        raise StorageError("Could not update daily acquisition settings") from exc
    finally:
        conn.close()


def _job_row(row) -> dict[str, object] | None:
    if row is None:
        return None
    started = row["started_at"]
    finished = row["finished_at"]
    duration_ms = None
    if started and finished:
        try:
            duration_ms = max(
                0,
                int(
                    (
                        datetime.fromisoformat(finished) - datetime.fromisoformat(started)
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
            existing = conn.execute(
                "SELECT * FROM daily_acquisition_jobs WHERE kind='automatic' AND schedule_date=?",
                (schedule_date,),
            ).fetchone()
            if existing is not None:
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
            "WHERE id=1 AND owner_token=? AND job_id=?",
            (now_text, _iso(now + timedelta(seconds=lease_seconds)), owner_token, job_id),
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
def set_current_instrument(job_id: str, code: str | None, *, db: Path | None = None) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
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
def set_item_running(job_id: str, kind: str, code: str, *, db: Path | None = None) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
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
    db: Path | None = None,
) -> None:
    if status not in ITEM_STATUSES:
        raise ValueError("Invalid daily acquisition item status")
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
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
        "SUM(status='failed') AS failed, SUM(status IN ('pending','running')) AS pending, "
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
        "unchanged=?, retry_count=? WHERE id=?",
        (
            int(row["total"] or 0),
            int(row["completed"] or 0),
            int(row["failed"] or 0),
            int(row["pending"] or 0),
            int(row["imports"] or 0),
            int(row["inserted"] or 0),
            int(row["updated"] or 0),
            int(row["unchanged"] or 0),
            int(row["retries"] or 0),
            job_id,
        ),
    )


@serialized_storage_write
def append_provider_error(job_id: str, error: dict[str, object], *, db: Path | None = None) -> None:
    conn = _write_connection(db if db is not None else database_path())
    try:
        with conn:
            row = _fetch_job(conn, job_id)
            errors = _json(row["provider_errors"] if row else "[]", [])
            if not isinstance(errors, list):
                errors = []
            safe = {
                "code": str(error.get("code", "provider_error"))[:64],
                "message": str(error.get("message", ""))[:240],
            }
            errors.append(safe)
            conn.execute(
                "UPDATE daily_acquisition_jobs SET provider_errors=?, last_error=? WHERE id=?",
                (json.dumps(errors[-50:], separators=(",", ":")), safe["message"] or None, job_id),
            )
    finally:
        conn.close()


@serialized_storage_write
def finish_job(
    job_id: str, *, status: str | None = None, db: Path | None = None
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
            _recalculate(conn, job_id)
            row = _fetch_job(conn, job_id)
            if status is None:
                if row["cancel_requested"]:
                    status = "cancelled"
                elif row["failed_instruments"] and not row["pending_instruments"]:
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
            "SELECT kind,code,provider FROM daily_acquisition_items WHERE job_id=? AND status='failed'",
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
    limit: int = 20,
    offset: int = 0,
    db: Path | None = None,
) -> dict[str, object]:
    if len(query) > 80 or len(provider) > 40 or len(status) > 20 or len(kind) > 20:
        raise ValueError("Invalid daily acquisition history filters")
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
            terms.append("j.provider=?")
            params.append(provider)
        if status:
            terms.append("j.status=?")
            params.append(status)
        if kind:
            terms.append("j.kind=?")
            params.append(kind)
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
