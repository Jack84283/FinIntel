"""Local monitoring API contract and write authorization tests."""

from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.daily_acquisition_store import create_job, finish_job, record_item


def test_daily_status_defaults_and_settings_are_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "daily.sqlite3"))
    monkeypatch.delenv("FININTEL_PROCESS_TOKEN", raising=False)

    with TestClient(app) as client:
        initial = client.get("/api/v1/daily-acquisition/status")
        assert initial.status_code == 200
        assert initial.json()["status"] == "idle"
        assert initial.json()["enabled"] is False
        assert initial.json()["timezone"] == "America/New_York"

        denied = client.put(
            "/api/v1/daily-acquisition/settings",
            json={"enabled": True},
        )
        assert denied.status_code == 403

        saved = client.put(
            "/api/v1/daily-acquisition/settings",
            headers={"x-finintel-client": "local-workstation"},
            json={"schedule_time": "20:30", "secondary_provider": None},
        )
        assert saved.status_code == 200
        assert saved.json()["schedule_time"] == "20:30"
        assert saved.json()["secondary_provider"] is None

        status = client.get("/api/v1/daily-acquisition/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["scheduled_execution_time"] == "20:30"
        assert payload["job"]["id"] is None
        assert isinstance(payload["next_scheduled_execution"], str)
        assert payload["duration_ms"] is None
        assert "provider_quota" in payload
        assert payload["provider_quota"]["alpaca_sip"]["requests_used"] == 0


def test_daily_history_rejects_invalid_execution_date(tmp_path, monkeypatch):
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "daily.sqlite3"))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/daily-acquisition/history", params={"execution_date": "not-a-date"}
        )
    assert response.status_code == 422


def test_failed_current_job_does_not_erase_last_successful_trading_session(tmp_path, monkeypatch):
    db = tmp_path / "daily.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    successful = create_job(
        kind="automatic",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-29T20:00:00+00:00",
        schedule_date="2026-10-29",
        equity_symbols=["AAPL"],
        index_codes=[],
        db=db,
    )
    record_item(
        str(successful["id"]),
        "equity",
        "AAPL",
        status="completed",
        provider="alpaca_sip",
        last_session="2026-10-29",
        db=db,
    )
    finish_job(str(successful["id"]), status="completed", db=db)
    failed = create_job(
        kind="manual",
        provider="alpaca_sip",
        secondary_provider=None,
        scheduled_for="2026-10-30T20:00:00+00:00",
        schedule_date=None,
        equity_symbols=["MSFT"],
        index_codes=[],
        db=db,
    )
    record_item(
        str(failed["id"]),
        "equity",
        "MSFT",
        status="failed",
        provider="alpaca_sip",
        error_type="ProviderUnavailable",
        db=db,
    )
    finish_job(str(failed["id"]), status="failed", db=db)

    with TestClient(app) as client:
        payload = client.get("/api/v1/daily-acquisition/status").json()

    assert payload["status"] == "failed"
    assert payload["last_successful_trading_session"] == "2026-10-29"
