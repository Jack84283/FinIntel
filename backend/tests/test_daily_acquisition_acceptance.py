"""Disposable issue #26 acceptance harness regressions."""

import importlib.util
import json
from pathlib import Path

import pytest

script = Path(__file__).resolve().parents[2] / "scripts" / "daily_acquisition_acceptance.py"
spec = importlib.util.spec_from_file_location("daily_acquisition_acceptance", script)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_synthetic_harness_is_disposable_and_passes(tmp_path, monkeypatch):
    normal = tmp_path / "normal-user.sqlite3"
    disposable = tmp_path / "acceptance.sqlite3"
    output = tmp_path / "report.json"
    monkeypatch.setenv("FININTEL_DB_PATH", str(normal))

    assert module.main(["--db-path", str(disposable), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["verdict"] == "pass"
    assert report["run"]["automatic_daily_acquisition_enabled"] is False
    assert report["run"]["credentials_logged"] is False
    assert all(check["status"] == "pass" for check in report["checks"])
    assert disposable.is_file()
    assert not normal.exists()


def test_harness_refuses_existing_database(tmp_path):
    database = tmp_path / "existing.sqlite3"
    database.write_bytes(b"not a database")

    assert module.main(["--db-path", str(database)]) == 1


def test_unconfigured_live_provider_is_blocked_without_request(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_configured", lambda _provider: False)

    result = module._live_equity(tmp_path / "acceptance.sqlite3", "tiingo", ["AAPL"])

    assert result["status"] == "blocked"
    assert "not configured" in result["summary"]


@pytest.mark.parametrize(
    "message",
    [
        "https://example.test/query?api_key=private-value",
        "token=private-value password=also-private",
    ],
)
def test_provider_diagnostics_are_redacted(message):
    redacted = module._redact_message(message)

    assert "private-value" not in redacted
    assert "also-private" not in redacted
