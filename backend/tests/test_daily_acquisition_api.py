"""Local monitoring API contract and write authorization tests."""

from fastapi.testclient import TestClient

from finintel.main import app


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
        assert "provider_quota" in payload
        assert payload["provider_quota"]["alpaca_sip"]["requests_used"] == 0


def test_daily_history_rejects_invalid_execution_date(tmp_path, monkeypatch):
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "daily.sqlite3"))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/daily-acquisition/history", params={"execution_date": "not-a-date"}
        )
    assert response.status_code == 422
