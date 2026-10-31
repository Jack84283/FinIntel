"""Durable daily acquisition state is restart-safe and idempotent."""

import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finintel.storage.db as storage_db
from finintel.market_data.daily_acquisition_store import (
    LeaseLostError,
    claim_job,
    create_job,
    finish_job,
    get_job,
    get_settings,
    heartbeat,
    list_history,
    merge_job_items,
    record_item,
    release_lock,
    retry_failed_job,
    set_current_instrument,
    set_item_running,
    set_job_pause,
    update_settings,
)


def test_settings_job_lifecycle_and_singleton_lease(tmp_path):
    db = tmp_path / "daily.sqlite3"

    settings = update_settings(
        {
            "enabled": True,
            "schedule_time": "20:15",
            "timezone": "America/New_York",
            "primary_provider": "alpaca_sip",
            "secondary_provider": None,
            "overlap_days": 5,
            "retry_attempts": 2,
            "retry_backoff_seconds": 0,
        },
        db=db,
    )
    assert settings["enabled"] is True
    assert get_settings(db=db)["schedule_time"] == "20:15"

    job = create_job(
        kind="automatic",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:15:00+00:00",
        schedule_date="2026-10-29",
        equity_symbols=["AAPL"],
        index_codes=["SP500"],
        db=db,
    )
    duplicate = create_job(
        kind="automatic",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:15:00+00:00",
        schedule_date="2026-10-29",
        equity_symbols=["AAPL"],
        index_codes=["SP500"],
        db=db,
    )
    assert duplicate["id"] == job["id"]
    assert claim_job(str(job["id"]), "worker-a", db=db)
    assert not claim_job(str(job["id"]), "worker-b", db=db)
    assert not heartbeat(str(job["id"]), "worker-b", db=db)
    assert heartbeat(str(job["id"]), "worker-a", db=db)

    set_current_instrument(str(job["id"]), "AAPL", db=db)
    set_item_running(str(job["id"]), "equity", "AAPL", db=db)
    record_item(
        str(job["id"]),
        "equity",
        "AAPL",
        status="completed",
        provider="alpaca_sip",
        last_session="2026-10-29",
        inserted=2,
        unchanged=3,
        db=db,
    )
    set_item_running(str(job["id"]), "index", "SP500", db=db)
    record_item(
        str(job["id"]),
        "index",
        "SP500",
        status="completed",
        provider="fred",
        last_session="2026-10-29",
        inserted=1,
        index_result={"code": "SP500", "inserted": 1},
        db=db,
    )
    finished = finish_job(str(job["id"]), db=db)
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["completed_instruments"] == 2
    assert finished["successful_imports"] == 2
    assert finished["inserted"] == 3
    assert finished["index_results"] == {"SP500": {"code": "SP500", "inserted": 1}}
    assert get_job(str(job["id"]), db=db)["items"]
    release_lock(str(job["id"]), "worker-a", db=db)


def test_pause_cancel_retry_and_history_filters(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    job = create_job(
        kind="manual",
        provider="tiingo",
        secondary_provider="alpaca_sip",
        scheduled_for="2026-10-29T23:00:00+00:00",
        schedule_date=None,
        equity_symbols=["MSFT"],
        index_codes=[],
        db=db,
    )
    job_id = str(job["id"])
    assert set_job_pause(job_id, True, db=db)["status"] == "paused"
    assert set_job_pause(job_id, False, db=db)["status"] == "running"
    set_item_running(job_id, "equity", "MSFT", db=db)
    record_item(
        job_id,
        "equity",
        "MSFT",
        status="failed",
        provider="alpaca_sip",
        error_type="ProviderUnavailable",
        error_message="temporary outage",
        validation_status="failed",
        db=db,
    )
    failed = finish_job(job_id, status="failed", db=db)
    assert failed["status"] == "failed"

    retry = retry_failed_job(job_id, db=db)
    assert retry is not None
    assert retry["kind"] == "retry_failed"
    assert retry["items"][0]["code"] == "MSFT"
    assert list_history(query="MSFT", status="failed", db=db)["total"] == 1

    cancelled = create_job(
        kind="manual",
        provider="tiingo",
        secondary_provider=None,
        scheduled_for="2026-10-29T23:01:00+00:00",
        schedule_date=None,
        equity_symbols=[],
        index_codes=["VIX"],
        db=db,
    )
    from finintel.market_data.daily_acquisition_store import cancel_job

    assert cancel_job(str(cancelled["id"]), db=db)["status"] == "cancelled"


def test_incomplete_items_are_partial_and_retryable(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    job = create_job(
        kind="automatic",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:00:00+00:00",
        schedule_date="2026-10-29",
        equity_symbols=["AAPL"],
        index_codes=[],
        db=db,
    )
    job_id = str(job["id"])
    assert claim_job(job_id, "worker-a", db=db)
    set_item_running(job_id, "equity", "AAPL", owner_token="worker-a", db=db)
    from finintel.market_data.daily_acquisition_store import get_job

    record_item(
        job_id,
        "equity",
        "AAPL",
        status="incomplete",
        provider="alpaca_sip",
        validation_status="incomplete",
        missing_sessions=2,
        owner_token="worker-a",
        db=db,
    )
    partial = finish_job(job_id, owner_token="worker-a", db=db)
    assert partial["status"] == "partial"
    assert partial["incomplete_instruments"] == 1
    retry = retry_failed_job(job_id, db=db)
    assert retry is not None
    assert retry["items"][0]["code"] == "AAPL"
    assert get_job(job_id, db=db)["items"][0]["status"] == "incomplete"


def test_expired_owner_cannot_update_job(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    job = create_job(
        kind="manual",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:00:00+00:00",
        schedule_date=None,
        equity_symbols=["AAPL"],
        index_codes=[],
        db=db,
    )
    job_id = str(job["id"])
    assert claim_job(job_id, "worker-a", db=db)
    from finintel.storage.db import _write_connection

    conn = _write_connection(db)
    try:
        conn.execute(
            "UPDATE daily_acquisition_lock SET expires_at=? WHERE id=1",
            ("2000-01-01T00:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(LeaseLostError):
        set_current_instrument(job_id, "AAPL", owner_token="worker-a", db=db)


def test_merge_job_items_is_idempotent_and_reopens_terminal_job(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    job = create_job(
        kind="automatic",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:00:00+00:00",
        schedule_date="2026-10-29",
        equity_symbols=["AAPL"],
        index_codes=[],
        db=db,
    )
    job_id = str(job["id"])
    assert claim_job(job_id, "worker-a", db=db)
    set_item_running(job_id, "equity", "AAPL", owner_token="worker-a", db=db)
    record_item(
        job_id,
        "equity",
        "AAPL",
        status="completed",
        owner_token="worker-a",
        db=db,
    )
    finish_job(job_id, owner_token="worker-a", db=db)
    merged = merge_job_items(
        job_id,
        provider="alpaca_sip",
        equity_symbols=["AAPL", "MSFT"],
        index_codes=[],
        db=db,
    )
    assert merged["status"] == "scheduled"
    assert merged["pending_instruments"] == 1
    again = merge_job_items(
        job_id,
        provider="alpaca_sip",
        equity_symbols=["AAPL", "MSFT"],
        index_codes=[],
        db=db,
    )
    assert again["total_instruments"] == 2


def test_get_settings_migrates_before_reading_legacy_schema(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    from finintel.storage.db import _write_connection

    conn = _write_connection(db)
    try:
        conn.execute("ALTER TABLE daily_acquisition_settings DROP COLUMN catch_up_limit")
        conn.execute("ALTER TABLE daily_acquisition_jobs DROP COLUMN incomplete_instruments")
        conn.execute("PRAGMA user_version=8")
        conn.commit()
    finally:
        conn.close()
    settings = get_settings(db=db)
    assert settings["catch_up_limit"] == 5


def test_schema_v9_migrates_provider_usage_without_touching_price_history(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    from finintel.storage.db import _write_connection

    conn = _write_connection(db)
    with conn:
        conn.execute("INSERT INTO instruments(symbol, created_at) VALUES ('AAPL', '2026-10-01')")
        cursor = conn.execute(
            "INSERT INTO series(symbol, provider, frequency, adjustment, data_kind) "
            "VALUES ('AAPL', 'alpha_vantage', 'daily', 'raw_unadjusted', 'equity')"
        )
        series_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(series_id, retrieved_at, imported_at, payload_sha256, source_url, "
            "provider_terms_url, first_session, last_session, observations, inserted, "
            "updated, unchanged, stale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                series_id,
                "2026-10-29T20:00:00+00:00",
                "2026-10-29T20:00:00+00:00",
                "sha256",
                "https://example.com/history",
                "https://example.com/terms",
                "2026-10-28",
                "2026-10-28",
                1,
                1,
                0,
                0,
                0,
            ),
        )
        ingestion_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO daily_prices "
            "(series_id, session_date, open, high, low, close, volume, retrieved_at, ingestion_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                series_id,
                "2026-10-28",
                "100",
                "101",
                "99",
                "100.5",
                10,
                "2026-10-29T20:00:00+00:00",
                ingestion_id,
            ),
        )
        conn.execute("DROP TABLE daily_acquisition_provider_usage")
        conn.execute("PRAGMA user_version=9")

    assert get_settings(db=db)["provider_limits"]["alpaca_sip"]["requests_per_minute"] == 180
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 12
        assert check.execute("SELECT close FROM daily_prices").fetchone()[0] == "100.5"
        assert (
            check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='daily_acquisition_provider_usage'"
            ).fetchone()
            is not None
        )


def test_provider_request_budget_and_cross_process_pacing_are_durable(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings(
        {
            "provider_limits": {
                "alpaca_sip": {"requests_per_minute": 60, "daily_request_budget": 2},
            }
        },
        db=db,
    )
    from finintel.market_data.daily_acquisition_store import (
        mark_provider_status,
        provider_usage_status,
        reserve_provider_request,
        reset_provider_status,
    )
    from finintel.market_data.provider import ProviderBudgetExhausted

    moment = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    first = reserve_provider_request("alpaca_sip", db=db, now=moment)
    second = reserve_provider_request("alpaca_sip", db=db, now=moment)
    assert first["request_count"] == 1
    assert second["request_count"] == 2
    assert second["wait_seconds"] == 1
    with pytest.raises(ProviderBudgetExhausted):
        reserve_provider_request("alpaca_sip", db=db, now=moment)
    assert (
        provider_usage_status(db=db, now=moment)["alpaca_sip"]["quota_state"] == "budget_exhausted"
    )
    mark_provider_status("alpaca_sip", "quota_exhausted", "test quota", db=db, now=moment)
    mark_provider_status("alpaca_sip", "available", db=db, now=moment)
    assert (
        provider_usage_status(db=db, now=moment)["alpaca_sip"]["quota_state"] == "budget_exhausted"
    )
    reset_provider_status("alpaca_sip", db=db, now=moment)
    mark_provider_status("alpaca_sip", "quota_exhausted", "upstream quota", db=db, now=moment)
    mark_provider_status("alpaca_sip", "available", db=db, now=moment)
    assert (
        provider_usage_status(db=db, now=moment)["alpaca_sip"]["quota_state"] == "quota_exhausted"
    )
    # The usage and exhaustion marker survive a fresh settings/status read.
    assert get_settings(db=db)["provider_limits"]["alpaca_sip"]["daily_request_budget"] == 2
    assert provider_usage_status(db=db, now=moment)["alpaca_sip"]["requests_used"] == 2
    reset_provider_status("alpaca_sip", db=db, now=moment)
    assert provider_usage_status(db=db, now=moment)["alpaca_sip"]["quota_state"] == "available"


def test_schema_v10_failure_rolls_back_and_succeeds_on_retry(tmp_path, monkeypatch):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE daily_acquisition_provider_usage")
        conn.execute("ALTER TABLE daily_acquisition_settings DROP COLUMN provider_limits")
        conn.execute("PRAGMA user_version=9")

    original_index = storage_db.SCHEMA_V10_INDEX
    monkeypatch.setattr(
        storage_db,
        "SCHEMA_V10_INDEX",
        "CREATE INDEX daily_acquisition_provider_usage_date "
        "ON daily_acquisition_provider_usage(missing_column)",
    )
    with pytest.raises(storage_db.StorageError, match="v10"):
        storage_db._write_connection(db)

    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='daily_acquisition_provider_usage'"
            ).fetchone()
            is None
        )
        assert "provider_limits" not in {
            row[1] for row in check.execute("PRAGMA table_info(daily_acquisition_settings)")
        }

    monkeypatch.setattr(storage_db, "SCHEMA_V10_INDEX", original_index)
    conn = storage_db._write_connection(db)
    conn.close()
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 12
        assert (
            check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='daily_acquisition_provider_usage'"
            ).fetchone()
            is not None
        )


@pytest.mark.parametrize(
    ("requests_per_minute", "minimum_wait"),
    ((5, 12.0), (60, 1.0), (180, 60 / 180)),
)
def test_provider_request_spacing_preserves_precision(tmp_path, requests_per_minute, minimum_wait):
    db = tmp_path / "daily.sqlite3"
    update_settings(
        {
            "provider_limits": {
                "alpaca_sip": {
                    "requests_per_minute": requests_per_minute,
                    "daily_request_budget": 10,
                }
            }
        },
        db=db,
    )
    moment = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    from finintel.market_data.daily_acquisition_store import (
        provider_usage_status,
        reserve_provider_request,
    )

    reserve_provider_request("alpaca_sip", db=db, now=moment)
    second = reserve_provider_request("alpaca_sip", db=db, now=moment)
    assert second["wait_seconds"] >= minimum_wait
    usage = provider_usage_status(db=db, now=moment)["alpaca_sip"]
    assert str(usage["last_request_at"]).endswith("+00:00")
    assert "." in str(usage["last_request_at"])


@pytest.mark.parametrize(
    ("requests_per_minute", "minimum_wait"),
    ((5, 12.0), (60, 1.0), (180, 60 / 180)),
)
def test_provider_request_spacing_crosses_utc_midnight(tmp_path, requests_per_minute, minimum_wait):
    db = tmp_path / "daily.sqlite3"
    update_settings(
        {
            "provider_limits": {
                "alpaca_sip": {
                    "requests_per_minute": requests_per_minute,
                    "daily_request_budget": 10,
                }
            }
        },
        db=db,
    )
    from finintel.market_data.daily_acquisition_store import (
        provider_usage_status,
        reserve_provider_request,
    )

    before_midnight = datetime(2026, 10, 29, 23, 59, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    first = reserve_provider_request("alpaca_sip", db=db, now=before_midnight)
    second = reserve_provider_request("alpaca_sip", db=db, now=after_midnight)

    assert first["usage_date"] == "2026-10-29"
    assert second["usage_date"] == "2026-10-30"
    assert second["wait_seconds"] >= max(0.0, minimum_wait - 1.0)
    first_slot = datetime.fromisoformat(
        str(provider_usage_status(db=db, now=before_midnight)["alpaca_sip"]["last_request_at"])
    )
    second_slot = datetime.fromisoformat(
        str(provider_usage_status(db=db, now=after_midnight)["alpaca_sip"]["last_request_at"])
    )
    assert second_slot - first_slot >= timedelta(seconds=minimum_wait)
    assert provider_usage_status(db=db, now=before_midnight)["alpaca_sip"]["requests_used"] == 1
    assert provider_usage_status(db=db, now=after_midnight)["alpaca_sip"]["requests_used"] == 1


def test_provider_request_spacing_uses_future_slot_scheduled_into_next_day(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings(
        {
            "provider_limits": {
                "alpaca_sip": {"requests_per_minute": 5, "daily_request_budget": 10},
            }
        },
        db=db,
    )
    from finintel.market_data.daily_acquisition_store import reserve_provider_request

    first = datetime(2026, 10, 29, 23, 59, 50, tzinfo=UTC)
    second = datetime(2026, 10, 29, 23, 59, 51, tzinfo=UTC)
    midnight = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    reserve_provider_request("alpaca_sip", db=db, now=first)
    scheduled = reserve_provider_request("alpaca_sip", db=db, now=second)
    next_day = reserve_provider_request("alpaca_sip", db=db, now=midnight)

    assert scheduled["wait_seconds"] >= 11.0
    # The second reservation occupies 00:00:02 UTC; the next one must be
    # reserved after that slot rather than overlapping it.
    assert next_day["wait_seconds"] >= 14.0


def _reserve_from_process(args):
    db, timestamp = args
    from finintel.market_data.daily_acquisition_store import reserve_provider_request

    result = reserve_provider_request(
        "alpaca_sip", db=Path(db), now=datetime.fromisoformat(timestamp)
    )
    return result["request_count"], result["wait_seconds"]


def test_provider_reservations_are_serialized_across_processes(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings(
        {
            "provider_limits": {
                "alpaca_sip": {"requests_per_minute": 180, "daily_request_budget": 10},
            }
        },
        db=db,
    )
    moment = datetime(2026, 10, 29, 20, 0, tzinfo=UTC).isoformat()
    context = multiprocessing.get_context("spawn")
    with context.Pool(2) as pool:
        reservations = pool.map(_reserve_from_process, [(str(db), moment)] * 2)
    assert sorted(count for count, _ in reservations) == [1, 2]
    assert max(wait for _, wait in reservations) >= 60 / 180


def test_history_filters_provider_from_item_and_execution_date(tmp_path):
    db = tmp_path / "daily.sqlite3"
    update_settings({}, db=db)
    job = create_job(
        kind="manual",
        provider="alpaca_sip",
        secondary_provider="tiingo",
        scheduled_for="2026-10-29T20:00:00+00:00",
        schedule_date=None,
        equity_symbols=["AAPL"],
        index_codes=[],
        db=db,
    )
    job_id = str(job["id"])
    set_item_running(job_id, "equity", "AAPL", db=db)
    record_item(job_id, "equity", "AAPL", status="completed", provider="tiingo", db=db)
    finish_job(job_id, status="completed", db=db)
    assert list_history(provider="tiingo", execution_date="2026-10-29", db=db)["total"] == 1
    assert list_history(provider="alpha_vantage", execution_date="2026-10-29", db=db)["total"] == 0
