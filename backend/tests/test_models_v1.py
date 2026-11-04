"""Issue #32 v1: deterministic built-in model, source lineage and strict execution gate.

All data are synthetic and stored in disposable local SQLite databases.
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finintel.main import app
from finintel.market_data.analytics import AnalyticsError
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.registry import (
    IMPLEMENTATION_REVISION,
    MAX_MODEL_OBSERVATIONS,
    MODEL_ID,
    MODEL_VERSION,
    ModelParameters,
    model_registry,
)
from finintel.storage.db import _write_connection

START = date(2026, 9, 1)


def _seed(path, count=35, *, code="SP500", step=2, first_date=START):
    conn = _write_connection(path)
    with conn:
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES (?,'fred',?,?,'2026-10-25T00:00:00+00:00',?)",
            [
                (
                    code,
                    (first_date + timedelta(days=i)).isoformat(),
                    str(100 + step * i),
                    f"https://fred.stlouisfed.org/series/{code}",
                )
                for i in range(count)
            ],
        )
    conn.close()


def _evaluate(db, *, count=35, **overrides):
    args = {
        "start": START,
        "end": START + timedelta(days=count - 1),
        "quality_policy": "allow_unverified",
        "db": db,
    }
    args.update(overrides)
    return evaluate_builtin_indicator(**args)


def test_allowlist_and_declared_parameter_contract():
    catalog = model_registry()
    assert catalog["registry_version"] == "models-registry-v1"
    assert len(catalog["models"]) == 1
    model = catalog["models"][0]
    assert model["model_id"] == MODEL_ID
    assert model["model_version"] == MODEL_VERSION
    assert model["implementation_revision"] == IMPLEMENTATION_REVISION
    assert model["execution"]["builtin_only"]
    assert model["execution"]["no_dynamic_code"]
    assert model["execution"]["no_backtest"]
    assert model["input_contract"]["point_in_time_membership"] == "not_used"
    assert "VIX" not in model["input_contract"]["allowed_codes"]
    assert MAX_MODEL_OBSERVATIONS == 2000
    with pytest.raises(ValidationError):
        ModelParameters(fast_window=10, slow_window=10)
    with pytest.raises(ValidationError):
        ModelParameters(fast_window=3, slow_window=4, module_path="/tmp/private.py")
    with pytest.raises(ValidationError):
        ModelParameters(fast_window="5", slow_window=20)


def test_reproducible_model_outputs_and_close_only_signal_order(tmp_path):
    db = tmp_path / "synthetic.sqlite"
    _seed(db)
    before_size = db.stat().st_size
    first = _evaluate(db)
    again = _evaluate(db)
    assert first["status"] == "completed"
    assert first["execution_contract"] == "model-research-evaluation-v1"
    assert first["reproducibility_sha256"] == again["reproducibility_sha256"]
    assert first["output_sha256"] == again["output_sha256"]
    assert first["points"] == again["points"]
    assert first["read_only"] and first["no_network"] and first["no_synthetic_sessions"]
    assert first["signal_after_close_only"] and not first["backtest_validated"]
    assert not first["persistent_job"]
    assert first["quality_warning"]
    assert first["lineage"]["quality_verified"] is False
    assert first["lineage"]["source_identity"]["return_semantics"] == "price_index"
    assert first["lineage"]["source_identity"]["adjustment"] == "price_index"
    assert len(first["points"]) == 35
    assert all(row["signal_after_close"] is None for row in first["points"][:19])
    assert all(row["signal_after_close"] == "long" for row in first["points"][19:])
    assert first["points"][20]["fast_sma"] == "136"
    assert first["points"][20]["slow_sma"] == "121"
    assert db.stat().st_size == before_size
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_daily_closes").fetchone()[0] == 35


def test_prefix_invariance_next_session_only_and_exact_decimal_values(tmp_path):
    db = tmp_path / "synthetic.sqlite"
    _seed(db, count=12)
    params = ModelParameters(fast_window=2, slow_window=3)
    first = _evaluate(db, count=6, parameters=params)
    next_result = _evaluate(db, count=12, parameters=params)
    assert next_result["points"][:6] == first["points"]
    assert first["points"][2]["fast_sma"] == "103"
    assert first["points"][2]["slow_sma"] == "102"
    assert first["points"][2]["signal_after_close"] == "long"
    assert first["lineage"]["last_observed_session"] == "2026-09-06"
    assert next_result["lineage"]["last_observed_session"] == "2026-09-12"
    assert first["reproducibility_sha256"] != next_result["reproducibility_sha256"]

    # Decimal averages must retain information below double precision.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='100.00000000000000000001' "
            "WHERE index_code='SP500' AND session_date='2026-09-01'"
        )
    precision = _evaluate(db, count=6, parameters=params)
    assert Decimal(precision["points"][2]["slow_sma"]) > Decimal(102)


def test_changed_revision_and_quality_policy_fail_closed(tmp_path):
    db = tmp_path / "synthetic.sqlite"
    _seed(db)
    original = _evaluate(db)
    expected = original["lineage"]["source_snapshot_sha256"]
    assert _evaluate(db, expected_snapshot_sha256=expected)["output_sha256"] == original[
        "output_sha256"
    ]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='120', "
            "retrieved_at='2026-10-31T00:00:00+00:00' "
            "WHERE index_code='SP500' AND session_date='2026-09-06'"
        )
    with pytest.raises(AnalyticsError, match="source snapshot changed"):
        _evaluate(db, expected_snapshot_sha256=expected)
    changed = _evaluate(db)
    assert changed["lineage"]["source_snapshot_sha256"] != expected
    assert changed["reproducibility_sha256"] != original["reproducibility_sha256"]
    with pytest.raises(AnalyticsError, match="quality scan"):
        _evaluate(db, quality_policy="require_complete")


def test_no_volatility_or_custom_model_or_inadequate_sample(tmp_path):
    db = tmp_path / "synthetic.sqlite"
    _seed(db, count=10)
    with pytest.raises(AnalyticsError, match="Insufficient"):
        _evaluate(db, count=10)
    with pytest.raises(AnalyticsError, match="Unsupported model"):
        _evaluate(db, count=10, model_id="../../private")
    with pytest.raises(AnalyticsError, match="Unsupported model"):
        _evaluate(db, count=10, model_version="2.0")
    with pytest.raises(AnalyticsError, match="volatility"):
        _evaluate(db, count=10, code="VIX")
    with pytest.raises(AnalyticsError, match="expected source snapshot hash"):
        _evaluate(db, count=10, expected_snapshot_sha256="invalid")


def test_resource_cap_and_absent_storage_do_not_create_data(tmp_path):
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(AnalyticsError, match="No local stored"):
        _evaluate(absent)
    assert not absent.exists()
    db = tmp_path / "large.sqlite"
    historical_start = date(2020, 1, 1)
    _seed(db, count=MAX_MODEL_OBSERVATIONS + 1, step=0, first_date=historical_start)
    with pytest.raises(AnalyticsError, match="2,000"):
        _evaluate(
            db,
            start=historical_start,
            end=historical_start + timedelta(days=MAX_MODEL_OBSERVATIONS),
        )


def test_managed_desktop_api_validation_and_response_contract(tmp_path, monkeypatch):
    db = tmp_path / "synthetic.sqlite"
    _seed(db)
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "models-test-managed-token-value-32bytes")
    payload = {
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "code": "SP500",
        "start": "2026-09-01",
        "end": "2026-10-05",
        "quality_policy": "allow_unverified",
        "parameters": {"fast_window": 5, "slow_window": 20},
    }
    with TestClient(app) as client:
        public = client.get("/api/v1/models/registry")
        forbidden = client.post("/api/v1/models/evaluate", json=payload)
        headers = {"x-finintel-process-token": "models-test-managed-token-value-32bytes"}
        accepted = client.post("/api/v1/models/evaluate", json=payload, headers=headers)
        denied = client.post(
            "/api/v1/models/evaluate",
            json={**payload, "module_path": "/tmp/private_model.py"},
            headers=headers,
        )
        vix = client.post(
            "/api/v1/models/evaluate", json={**payload, "code": "VIX"}, headers=headers
        )
        wrong_version = client.post(
            "/api/v1/models/evaluate", json={**payload, "model_version": "1.0.1"}, headers=headers
        )
    assert public.status_code == 403  # A configured process token protects all routes.
    assert forbidden.status_code == 403
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["observations"] == 35
    assert result["lineage"]["parameters"] == {"fast_window": 5, "slow_window": 20}
    assert result["backtest_validated"] is False
    assert denied.status_code == 422
    assert vix.status_code == 422
    assert wrong_version.status_code == 422
    assert not any("private_model.py" in str(row) for row in result["points"])


def test_api_registry_has_only_fixed_model(tmp_path, monkeypatch):
    monkeypatch.delenv("FININTEL_PROCESS_TOKEN", raising=False)
    with TestClient(app) as client:
        registry = client.get("/api/v1/models/registry")
        forbidden = client.post(
            "/api/v1/models/evaluate",
            json={"start": "2026-09-01", "end": "2026-10-05"},
        )
    assert registry.status_code == 200
    assert registry.json()["models"][0]["model_id"] == MODEL_ID
    assert forbidden.status_code == 403
