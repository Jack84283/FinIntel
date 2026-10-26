"""Secure native provider credential API regression tests without a live OS keychain."""
import keyring
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.settings.credentials import get_secret


def test_provider_secrets_are_never_returned(monkeypatch):
    vault = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, name: vault.get((service, name)))
    monkeypatch.setattr(
        keyring, "set_password", lambda service, name, value: vault.__setitem__((service, name), value)
    )
    monkeypatch.setattr(
        keyring, "delete_password", lambda service, name: vault.pop((service, name), None)
    )
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "test-native-token-with-at-least-32-chars")
    headers = {"x-finintel-process-token": "test-native-token-with-at-least-32-chars"}
    with TestClient(app) as client:
        denied = client.put(
            "/api/v1/settings/providers/tiingo/api_key", json={"value": "private-secret"}
        )
        assert denied.status_code == 403
        updated = client.put(
            "/api/v1/settings/providers/tiingo/api_key",
            headers=headers,
            json={"value": "private-secret"},
        )
        assert updated.status_code == 200
        assert "private-secret" not in updated.text
        state = client.get("/api/v1/settings/providers", headers=headers)
        assert state.status_code == 200
        assert state.json()["tiingo"]["api_key"] is True
        assert "private-secret" not in state.text
        deleted = client.delete(
            "/api/v1/settings/providers/tiingo/api_key", headers=headers
        )
        assert deleted.status_code == 200
        assert client.get("/api/v1/settings/providers", headers=headers).json()["tiingo"][
            "api_key"
        ] is False


def test_packaged_backend_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "test-native-token-with-at-least-32-chars")
    monkeypatch.setenv("FININTEL_TIINGO_API_KEY", "not-from-environment")
    monkeypatch.setattr(keyring, "get_password", lambda _service, _name: None)
    assert get_secret("tiingo", "api_key") == ""


def test_invalid_provider_field_rejected_without_key_exposure(monkeypatch):
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "test-native-token-with-at-least-32-chars")
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/providers/tiingo/not_allowed",
            headers={"x-finintel-process-token": "test-native-token-with-at-least-32-chars"},
            json={"value": "private-secret"},
        )
    assert response.status_code == 422
    assert "private-secret" not in response.text
