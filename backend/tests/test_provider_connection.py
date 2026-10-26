"""Provider connection checks never store market data or reveal credentials."""
import httpx
import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.settings import connection_test

TOKEN = "test-native-token-with-at-least-32-chars"


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("alpha_vantage", {"bestMatches": []}),
        ("alpaca_sip", {"bars": {"AAPL": []}}),
        ("tiingo", {"ticker": "aapl", "name": "Apple"}),
        ("equibles", {"data": []}),
    ],
)
def test_provider_success_uses_saved_key_read_only(monkeypatch, provider, payload):
    monkeypatch.setattr(connection_test, "get_secret", lambda _provider, _field: "fake-key")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as session:
        result = connection_test.test_connection(provider, client=session)
    assert result["status"] == "connected"
    assert result["http_status"] == 200
    assert len(requests) == 1
    assert "fake-key" not in str(result)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(401, "authentication_failed"), (403, "limited"), (429, "rate_limited"), (503, "unreachable")],
)
def test_bounded_http_diagnostics_are_sanitized(monkeypatch, code, expected):
    monkeypatch.setattr(connection_test, "get_secret", lambda _provider, _field: "private-key")
    with httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(code, json={"secret": "private-key"})
    )) as session:
        result = connection_test.test_connection("tiingo", client=session)
    assert result["status"] == expected
    assert "private-key" not in str(result)


def test_endpoint_requires_process_authorization(monkeypatch):
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    monkeypatch.setattr(connection_test, "test_connection", lambda provider: {"status": "connected", "provider": provider})
    with TestClient(app) as client:
        denied = client.post("/api/v1/settings/providers/tiingo/test")
        valid = client.post(
            "/api/v1/settings/providers/tiingo/test",
            headers={"x-finintel-process-token": TOKEN},
        )
    assert denied.status_code == 403
    assert valid.status_code == 200
    assert valid.json()["status"] == "connected"


def test_missing_required_provider_keys_skips_network(monkeypatch):
    monkeypatch.setattr(connection_test, "get_secret", lambda provider, field: "" if field == "secret_key" else "some-key")
    result = connection_test.test_connection("alpaca_sip")
    assert result["status"] == "not_configured"
    assert result["http_status"] is None
