"""Local validation job API behavior without scanning a production database."""

import time

from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data import validation_api

TOKEN = "t" * 32


def managed_client(monkeypatch):
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    return TestClient(app, headers={"x-finintel-process-token": TOKEN})


def test_validation_start_and_result(monkeypatch, tmp_path):
    db = tmp_path / "local.sqlite3"
    db.touch()
    monkeypatch.setattr(validation_api, "database_path", lambda: db)
    monkeypatch.setattr(validation_api, "validate", lambda path: {"price_rows": 2, "issues": {}})
    monkeypatch.setattr(validation_api, "run_quality_scan", lambda path: {"finding_count": 0})
    with validation_api._lock:
        validation_api._job = None
    client = managed_client(monkeypatch)
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
    response = managed_client(monkeypatch).post(
        "/api/v1/validation/run", json={"checks": ["coverage"]}
    )
    assert response.status_code == 503


def test_reference_action_explicit(monkeypatch, tmp_path):
    db = tmp_path / "prices.sqlite3"
    db.touch()
    monkeypatch.setattr(validation_api, "database_path", lambda: db)
    monkeypatch.setattr(
        validation_api, "verify_events", lambda path, symbol: {"symbol": symbol, "events": []}
    )
    client = managed_client(monkeypatch)
    assert (
        client.post("/api/v1/validation/corporate-reference", json={"symbol": "SPY"}).status_code
        == 200
    )
    assert (
        client.post("/api/v1/validation/corporate-reference", json={"symbol": ""}).status_code
        == 422
    )


def test_all_six_checks_run_and_return_expected_sections(monkeypatch, tmp_path):
    db = tmp_path / "prices.sqlite3"
    db.touch()
    monkeypatch.setattr(validation_api, "database_path", lambda: db)
    monkeypatch.setattr(validation_api, "validate", lambda path: {"price_rows": 2, "issues": {}})
    diagnostic = {
        "series_count": 1,
        "series_without_prices": 0,
        "coverage_examples": [],
        "weekday_gap_candidates": 0,
        "series_with_weekday_gap_candidates": 0,
        "gap_examples": [],
        "xnys_internal_gap_candidates": 0,
        "series_with_xnys_internal_gaps": 0,
        "xnys_gap_examples": [],
        "reference_symbol": "SPY",
        "reference_provider": "alpaca_sip",
        "reference_session_gap_candidates": 0,
        "series_with_reference_session_gaps": 0,
        "reference_gap_examples": [],
        "observed_span_groups": {"under_1_year": 1},
        "series_with_less_than_9_years_observed": 1,
        "corporate_action_jump_candidate_count": 0,
        "corporate_action_jump_candidates": [],
        "cross_provider_close_divergences_over_1pct": 0,
        "cross_provider_pairs_compared": 0,
        "cross_provider_comparisons": [],
        "limitations": ["Sample fixture"],
    }
    monkeypatch.setattr(validation_api, "diagnostics", lambda path: diagnostic)
    monkeypatch.setattr(
        validation_api,
        "benchmark",
        lambda path: {"all_acceptance_targets_met": True, "sample_count": 1},
    )
    monkeypatch.setattr(
        validation_api,
        "run_quality_scan",
        lambda path: {"status": "completed", "finding_count": 0, "examples": []},
    )
    with validation_api._lock:
        validation_api._job = None
    client = managed_client(monkeypatch)
    started = client.post("/api/v1/validation/run", json={"checks": list(validation_api.CHECKS)})
    assert started.status_code == 200
    for _ in range(100):
        status = client.get("/api/v1/validation/status").json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(0.01)
    assert status["status"] == "completed", status.get("error")
    assert set(status["results"]) == {
        "integrity",
        "coverage",
        "corporate",
        "provider",
        "performance",
        "quality",
        "limitations",
    }
    assert status["results"]["coverage"]["xnys_internal_gap_candidates"] == 0
    assert status["results"]["performance"]["all_acceptance_targets_met"]
