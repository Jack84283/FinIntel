"""No-network tests for calendar-aware scheduling and monitoring."""

import threading
from datetime import UTC, date, datetime

import pytest

from finintel.market_data import daily_scheduler
from finintel.market_data.daily_scheduler import (
    DailyAcquisitionScheduler,
    automatic_schedule_dates,
    due_date,
    expected_missing_sessions,
    is_trading_day,
    latest_provider_eligible_session,
    missing_sessions_for_instrument,
    next_scheduled_execution,
    save,
    status,
)


def _seed_sessions(db, symbol, provider, sessions):
    from finintel.storage.db import _write_connection

    conn = _write_connection(db)
    try:
        conn.execute("INSERT INTO instruments(symbol, created_at) VALUES (?, ?)", (symbol, ""))
        series = conn.execute(
            "INSERT INTO series(symbol, provider, frequency, adjustment, data_kind) "
            "VALUES (?, ?, '1d', 'raw_unadjusted', 'equity') RETURNING id",
            (symbol, provider),
        ).fetchone()[0]
        ingestion = conn.execute(
            "INSERT INTO ingestion_runs(series_id, retrieved_at, imported_at, payload_sha256, "
            "source_url, provider_terms_url, provider_last_refreshed, first_session, last_session, "
            "observations, inserted, updated, unchanged, stale) VALUES (?, '', '', '', '', '', "
            "NULL, ?, ?, ?, 0, 0, 0, 0) RETURNING id",
            (series, min(sessions).isoformat(), max(sessions).isoformat(), len(sessions)),
        ).fetchone()[0]
        conn.executemany(
            "INSERT INTO daily_prices(series_id, session_date, open, high, low, close, volume, "
            "retrieved_at, ingestion_id) VALUES (?, ?, '1', '1', '1', '1', 0, '', ?)",
            ((series, session.isoformat(), ingestion) for session in sessions),
        )
        conn.commit()
    finally:
        conn.close()


def test_after_close_eastern_standard_time():
    assert due_date(datetime(2026, 11, 2, 1, 0, tzinfo=UTC)) is None
    assert due_date(datetime(2026, 11, 3, 1, 0, tzinfo=UTC)) == "2026-11-02"


def test_after_close_eastern_daylight_time():
    assert due_date(datetime(2026, 10, 28, 23, 0, tzinfo=UTC)) is None
    assert due_date(datetime(2026, 10, 29, 0, 0, tzinfo=UTC)) == "2026-10-28"


def test_weekend_has_no_scheduled_run():
    assert due_date(datetime(2026, 10, 31, 23, 0, tzinfo=UTC)) is None


def test_nyse_holiday_has_no_scheduled_run():
    assert not is_trading_day(date(2026, 7, 3))
    assert due_date(datetime(2026, 7, 4, 0, 30, tzinfo=UTC)) is None


def test_next_execution_and_missing_sessions_use_exchange_calendar():
    upcoming = next_scheduled_execution(
        datetime(2026, 7, 3, 1, 0, tzinfo=UTC), "20:00", "America/New_York"
    )
    assert upcoming.isoformat().startswith("2026-07-07T00:00:00")
    assert expected_missing_sessions(date(2026, 7, 1), date(2026, 7, 6)) == 2


def test_missing_sessions_uses_stored_provider_dates_and_listing_boundary(tmp_path, monkeypatch):
    db = tmp_path / "daily.sqlite3"
    from finintel.market_data.daily_acquisition_store import update_settings

    update_settings({}, db=db)
    _seed_sessions(db, "AAPL", "alpaca_sip", [date(2026, 7, 1), date(2026, 7, 6)])
    monkeypatch.setattr("finintel.market_data.daily_scheduler.database_path", lambda: db)
    assert missing_sessions_for_instrument("AAPL", "alpaca_sip", end_date=date(2026, 7, 6)) == {
        date(2026, 7, 2)
    }
    _seed_sessions(db, "MSFT", "alpaca_sip", [date(2026, 7, 6)])
    assert missing_sessions_for_instrument("MSFT", "alpaca_sip", end_date=date(2026, 7, 6)) == set()


def test_stale_provider_response_is_incomplete_through_expected_session(tmp_path, monkeypatch):
    db = tmp_path / "daily.sqlite3"
    from finintel.market_data.daily_acquisition_store import update_settings

    update_settings({}, db=db)
    _seed_sessions(db, "AAPL", "alpaca_sip", [date(2026, 7, 1), date(2026, 7, 2)])
    monkeypatch.setattr("finintel.market_data.daily_scheduler.database_path", lambda: db)
    assert missing_sessions_for_instrument("AAPL", "alpaca_sip", end_date=date(2026, 7, 2)) == set()
    assert missing_sessions_for_instrument("AAPL", "alpaca_sip", end_date=date(2026, 7, 6)) == {
        date(2026, 7, 6)
    }
    assert latest_provider_eligible_session("tiingo", date(2026, 7, 6)) == date(2026, 7, 2)


def test_automatic_schedule_dates_catches_up_with_limit_and_never_runs_early(monkeypatch):
    settings = {
        "schedule_time": "20:00",
        "timezone": "America/New_York",
        "catch_up_limit": 2,
    }
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.list_automatic_jobs",
        lambda **_: [{"schedule_date": "2026-10-26", "status": "completed"}],
    )
    before_close = automatic_schedule_dates(settings, now=datetime(2026, 10, 29, 23, 0, tzinfo=UTC))
    assert before_close == [date(2026, 10, 27), date(2026, 10, 28)]
    after_close = automatic_schedule_dates(settings, now=datetime(2026, 10, 30, 0, 0, tzinfo=UTC))
    assert after_close == [date(2026, 10, 28), date(2026, 10, 29)]


def test_automatic_schedule_dates_recovers_missed_work_on_weekend(monkeypatch):
    settings = {
        "schedule_time": "20:00",
        "timezone": "America/New_York",
        "catch_up_limit": 2,
    }
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.list_automatic_jobs",
        lambda **_: [{"schedule_date": "2026-10-29", "status": "completed"}],
    )
    assert automatic_schedule_dates(settings, now=datetime(2026, 10, 31, 18, 0, tzinfo=UTC)) == [
        date(2026, 10, 30)
    ]


def test_automatic_schedule_dates_preserves_existing_incomplete_work(monkeypatch):
    settings = {
        "schedule_time": "20:00",
        "timezone": "America/New_York",
        "catch_up_limit": 1,
    }
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.list_automatic_jobs",
        lambda **_: [
            {"schedule_date": "2026-10-27", "status": "partial"},
            {"schedule_date": "2026-10-28", "status": "completed"},
        ],
    )
    assert automatic_schedule_dates(settings, now=datetime(2026, 10, 30, 0, 0, tzinfo=UTC)) == [
        date(2026, 10, 27),
        date(2026, 10, 29),
    ]


def test_status_round_trip(tmp_path):
    path = tmp_path / "daily.json"
    assert status(path)["status"] == "never_run"
    save(path, {"status": "complete", "last_completed_date": "2026-10-28"})
    assert status(path)["last_completed_date"] == "2026-10-28"


def test_explicit_manual_and_retry_recovery_resets_fred(monkeypatch):
    settings = {
        "primary_provider": "alpaca_sip",
        "secondary_provider": "tiingo",
    }
    reset_calls = []
    launched = []
    monkeypatch.setattr("finintel.market_data.daily_scheduler.get_settings", lambda: settings)
    monkeypatch.setattr("finintel.market_data.daily_scheduler.get_active_job", lambda: None)
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.reset_provider_status",
        lambda provider: reset_calls.append(provider),
    )
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.instruments", lambda _provider: ["AAPL"]
    )
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.create_job", lambda **_: {"id": "manual-job"}
    )
    monkeypatch.setattr("finintel.market_data.daily_scheduler.get_job", lambda *_: None)
    monkeypatch.setattr(
        "finintel.market_data.daily_scheduler.retry_failed_job",
        lambda _job_id: {
            "id": "retry-job",
            "provider": "alpha_vantage",
            "secondary_provider": None,
            "items": [
                {"kind": "equity", "provider": "tiingo"},
                {"kind": "index", "provider": "fred"},
            ],
        },
    )
    scheduler = DailyAcquisitionScheduler()
    monkeypatch.setattr(scheduler, "_launch", lambda job_id: launched.append(job_id))

    assert scheduler.trigger_manual()["id"] == "manual-job"
    assert scheduler.retry_failed("old-job")["id"] == "retry-job"

    assert reset_calls == [
        "alpaca_sip",
        "fred",
        "tiingo",
        "alpha_vantage",
        "tiingo",
        "fred",
    ]
    assert launched == ["manual-job", "retry-job"]


def test_scheduled_request_guard_stops_before_http_on_cancellation(monkeypatch):
    scheduler = DailyAcquisitionScheduler()
    lease_lost = threading.Event()
    monkeypatch.setattr(
        daily_scheduler,
        "reserve_provider_request",
        lambda _provider: {"wait_seconds": 60},
    )
    monkeypatch.setattr(
        scheduler,
        "_wait_request_slot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(daily_scheduler._AcquisitionStopped()),
    )

    with pytest.raises(daily_scheduler._AcquisitionStopped):
        scheduler._request_guard("alpaca_sip", "job-id", lease_lost)()


def test_scheduled_request_guard_revalidates_after_pause(monkeypatch):
    scheduler = DailyAcquisitionScheduler()
    lease_lost = threading.Event()
    reservations = []
    paused = iter((True, False))
    monkeypatch.setattr(
        daily_scheduler,
        "reserve_provider_request",
        lambda provider: reservations.append(provider) or {"wait_seconds": 0},
    )
    monkeypatch.setattr(scheduler, "_wait_request_slot", lambda *_args, **_kwargs: next(paused))

    scheduler._request_guard("alpaca_sip", "job-id", lease_lost)()

    assert reservations == ["alpaca_sip", "alpaca_sip"]


def test_scheduled_shutdown_propagates_out_of_index_handler(monkeypatch):
    scheduler = DailyAcquisitionScheduler()
    scheduler._stop.set()
    monkeypatch.setattr(daily_scheduler, "set_item_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        daily_scheduler,
        "reserve_provider_request",
        lambda _provider: {"wait_seconds": 0},
    )

    with pytest.raises(daily_scheduler._AcquisitionStopped) as caught:
        scheduler._run_index(
            "job-id",
            "SP500",
            {"retry_attempts": 1, "retry_backoff_seconds": 0},
            "owner",
            threading.Event(),
        )

    assert caught.value.shutdown is True
