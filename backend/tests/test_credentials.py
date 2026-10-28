"""Secure native provider credential API tests without a live OS keychain."""

import keyring
from fastapi.testclient import TestClient
from keyring.errors import NoKeyringError

from finintel.main import app
from finintel.settings.credentials import get_secret


def test_provider_secrets_are_never_returned(monkeypatch):
    vault = {}

    def read_password(service, name):
        return vault.get((service, name))

    def write_password(service, name, value):
        vault[(service, name)] = value

    def remove_password(service, name):
        vault.pop((service, name), None)

    monkeypatch.setattr(keyring, "get_password", read_password)
    monkeypatch.setattr(keyring, "set_password", write_password)
    monkeypatch.setattr(keyring, "delete_password", remove_password)
    token = "test-native-token-with-at-least-32-chars"
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    headers = {"x-finintel-process-token": token}
    url = "/api/v1/settings/providers/tiingo/api_key"
    with TestClient(app) as client:
        denied = client.put(url, json={"value": "private-secret"})
        assert denied.status_code == 403
        updated = client.put(url, headers=headers, json={"value": "private-secret"})
        assert updated.status_code == 200
        assert "private-secret" not in updated.text
        status = client.get("/api/v1/settings/providers", headers=headers)
        assert status.status_code == 200
        assert status.json()["tiingo"]["api_key"] is True
        assert "private-secret" not in status.text
        deleted = client.delete(url, headers=headers)
        assert deleted.status_code == 200
        remaining = client.get("/api/v1/settings/providers", headers=headers).json()
        assert remaining["tiingo"]["api_key"] is False


def test_packaged_backend_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "test-native-token-with-at-least-32-chars")
    monkeypatch.setenv("FININTEL_TIINGO_API_KEY", "not-from-environment")
    monkeypatch.setattr(keyring, "get_password", lambda _service, _name: None)
    assert get_secret("tiingo", "api_key") == ""


def test_invalid_provider_field_rejected_without_key_exposure(monkeypatch):
    token = "test-native-token-with-at-least-32-chars"
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/providers/tiingo/not_allowed",
            headers={"x-finintel-process-token": token},
            json={"value": "private-secret"},
        )
    assert response.status_code == 422
    assert "private-secret" not in response.text


def test_unavailable_os_vault_is_not_misreported_as_missing_credentials(monkeypatch):
    token = "test-native-token-with-at-least-32-chars"
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)

    def unavailable(_service, _name):
        raise NoKeyringError("No secure vault available")

    monkeypatch.setattr(keyring, "get_password", unavailable)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/settings/providers",
            headers={"x-finintel-process-token": token},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "OS secure credential storage unavailable"
    assert "No secure vault available" not in response.text
