"""No-network tests for Eastern-time scheduling and monitoring."""

from datetime import UTC, datetime

from finintel.market_data.daily_scheduler import due_date, save, status


def test_after_close_eastern_standard_time():
    assert due_date(datetime(2026, 11, 2, 1, 0, tzinfo=UTC)) is None
    assert due_date(datetime(2026, 11, 3, 1, 0, tzinfo=UTC)) == "2026-11-02"


def test_after_close_eastern_daylight_time():
    assert due_date(datetime(2026, 10, 28, 23, 0, tzinfo=UTC)) is None
    assert due_date(datetime(2026, 10, 29, 0, 0, tzinfo=UTC)) == "2026-10-28"


def test_weekend_has_no_scheduled_run():
    assert due_date(datetime(2026, 10, 31, 23, 0, tzinfo=UTC)) is None


def test_status_round_trip(tmp_path):
    path = tmp_path / "daily.json"
    assert status(path)["status"] == "never_run"
    save(path, {"status": "complete", "last_completed_date": "2026-10-28"})
    assert status(path)["last_completed_date"] == "2026-10-28"
