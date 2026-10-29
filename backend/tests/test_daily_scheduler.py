"""No-network tests for calendar-aware scheduling and monitoring."""

from datetime import UTC, date, datetime

from finintel.market_data.daily_scheduler import (
    due_date,
    expected_missing_sessions,
    is_trading_day,
    next_scheduled_execution,
    save,
    status,
)


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


def test_status_round_trip(tmp_path):
    path = tmp_path / "daily.json"
    assert status(path)["status"] == "never_run"
    save(path, {"status": "complete", "last_completed_date": "2026-10-28"})
    assert status(path)["last_completed_date"] == "2026-10-28"
