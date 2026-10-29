"""Durable daily acquisition state is restart-safe and idempotent."""

import pytest

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
