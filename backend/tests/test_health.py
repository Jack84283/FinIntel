"""Bootstrap contract checks for the local Python API."""

from fastapi.testclient import TestClient

from finintel.main import app


def test_health_contract() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "finintel-backend",
        "version": "0.1.0",
    }


def test_no_market_data_routes_in_bootstrap() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/markets/overview")

    assert response.status_code == 404
