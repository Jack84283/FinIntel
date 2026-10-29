"""Durable daily acquisition state is restart-safe and idempotent."""

from finintel.market_data.daily_acquisition_store import (
    claim_job,
    create_job,
    finish_job,
    get_job,
    get_settings,
    heartbeat,
    list_history,
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
