"""Local validation job API behavior without scanning a production database."""

import time

from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data import validation_api


def test_validation_start_and_result(monkeypatch, tmp_path):
    db = tmp_path / "local.sqlite3"
    db.touch()
    monkeypatch.setattr(validation_api, "database_path", lambda: db)
    monkeypatch.setattr(validation_api, "validate", lambda path: {"price_rows": 2, "issues": {}})
    with validation_api._lock:
        validation_api._job = None
    client = TestClient(app)
    invalid = client.post("/api/v1/validation/run", json={"checks": ["invalid"]})
    assert invalid.status_code == 422
    started = client.post("/api/v1/validation/run", json={"checks": ["integrity"]})
    assert started.status_code == 200
    for _ in range(100):
        status = client.get("/api/v1/validation/status").json()
        if status["status"] == "completed":
            break
        time.sleep(0.01)
    assert status["status"] == "completed"
    assert status["results"]["integrity"]["price_rows"] == 2


def test_validation_requires_local_db(monkeypatch, tmp_path):
    monkeypatch.setattr(validation_api, "database_path", lambda: tmp_path / "missing.sqlite3")
    response = TestClient(app).post("/api/v1/validation/run", json={"checks": ["coverage"]})
    assert response.status_code == 503


def test_reference_action_explicit(monkeypatch, tmp_path):
    db = tmp_path / "prices.sqlite3"
    db.touch()
    monkeypatch.setattr(validation_api, "database_path", lambda: db)
    monkeypatch.setattr(
        validation_api, "verify_events", lambda path, symbol: {"symbol": symbol, "events": []}
    )
    client = TestClient(app)
    assert (
        client.post("/api/v1/validation/corporate-reference", json={"symbol": "SPY"}).status_code
        == 200
    )
    assert client.post("/api/v1/validation/corporate-reference", json={"symbol": ""}).status_code == 422
