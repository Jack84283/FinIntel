"""CLI regression: optional provider errors must not discard the local audit."""

import importlib.util
import json
import sys
from pathlib import Path

script = Path(__file__).resolve().parents[2] / "scripts" / "validate_local.py"
spec = importlib.util.spec_from_file_location("validate_local", script)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_optional_tiingo_failure_preserves_completed_report(monkeypatch, tmp_path):
    output = tmp_path / "acceptance-report.json"
    results = {
        "integrity": {"issues": {}},
        "coverage": {"xnys_internal_gap_candidates": 5},
        "corporate": {"corporate_action_jump_candidate_count": 0},
        "provider": {"cross_provider_pairs_compared": 4},
        "performance": {"all_acceptance_targets_met": True},
    }

    def mock_request(endpoint, payload=None):
        if endpoint == "/run":
            return {"id": "test-job", "status": "running"}
        if endpoint == "/status":
            return {"id": "test-job", "status": "completed", "results": results}
        if endpoint == "/corporate-reference":
            raise RuntimeError("Backend HTTP 422: missing Tiingo key")
        raise AssertionError(endpoint)

    monkeypatch.setattr(module, "request", mock_request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_local.py", "--output", str(output), "--corporate-symbol", "AAPL"],
    )
    assert module.main() == 2
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["integrity"]["issues"] == {}
    assert saved["coverage"]["xnys_internal_gap_candidates"] == 5
    assert "missing Tiingo key" in saved["corporate_reference_error"]


def test_without_optional_reference_keeps_success_exit(monkeypatch, tmp_path):
    output = tmp_path / "report.json"

    def mock_request(endpoint, payload=None):
        if endpoint == "/run":
            return {"id": "test-job", "status": "running"}
        if endpoint == "/status":
            return {
                "id": "test-job",
                "status": "completed",
                "results": {
                    "integrity": {"issues": {}},
                    "coverage": {"xnys_internal_gap_candidates": 0},
                    "provider": {"cross_provider_pairs_compared": 1},
                    "performance": {"all_acceptance_targets_met": True},
                },
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(module, "request", mock_request)
    monkeypatch.setattr(sys, "argv", ["validate_local.py", "--output", str(output)])
    assert module.main() == 0
    assert output.is_file()


def test_409_joins_compatible_active_validation(monkeypatch, tmp_path):
    output = tmp_path / "joined.json"
    calls = []

    def mock_request(endpoint, payload=None):
        calls.append(endpoint)
        if endpoint == "/run":
            raise RuntimeError('Backend HTTP 409: {"detail":"A validation job is already running."}')
        if endpoint == "/status":
            if calls.count("/status") == 1:
                return {"id": "existing-job", "status": "running", "checks": module.CHECKS}
            return {
                "id": "existing-job",
                "status": "completed",
                "checks": module.CHECKS,
                "results": {"integrity": {"issues": {}}, "coverage": {}, "provider": {}, "performance": {}},
            }
        if endpoint == "/corporate-reference":
            return {"symbol": "AAPL", "events": [], "event_count": 0}
        raise AssertionError(endpoint)

    monkeypatch.setattr(module, "request", mock_request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_local.py", "--output", str(output), "--corporate-symbol", "AAPL"],
    )
    assert module.main() == 0
    assert json.loads(output.read_text())["corporate_reference"]["symbol"] == "AAPL"
    assert calls.count("/run") == 1


def test_409_rejects_incompatible_checks(monkeypatch):
    def mock_request(endpoint, payload=None):
        if endpoint == "/run":
            raise RuntimeError("Backend HTTP 409: conflict")
        return {"id": "other", "status": "running", "checks": ["integrity"]}

    monkeypatch.setattr(module, "request", mock_request)
    import pytest

    with pytest.raises(RuntimeError, match="different checks"):
        module.start_or_join()


def test_join_rejects_replaced_job(monkeypatch, tmp_path):
    def mock_request(endpoint, payload=None):
        if endpoint == "/run":
            return {"id": "original", "status": "running"}
        return {"id": "replacement", "status": "completed", "results": {}}

    monkeypatch.setattr(module, "request", mock_request)
    monkeypatch.setattr(
        sys, "argv", ["validate_local.py", "--output", str(tmp_path / "wrong.json")]
    )
    import pytest

    with pytest.raises(RuntimeError, match="job changed"):
        module.main()
