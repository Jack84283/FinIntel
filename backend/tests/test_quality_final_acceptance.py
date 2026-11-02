"""Issue #29 acceptance harness contract: no implicit user-data writes or live calls."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality_acceptance.py"
SPEC = importlib.util.spec_from_file_location("quality_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_disposable_synthetic_acceptance_never_opens_configured_user_db(tmp_path, monkeypatch):
    production = tmp_path / "user-market.sqlite3"
    output = tmp_path / "synthetic-report.json"
    monkeypatch.setenv("FININTEL_DB_PATH", str(production))
    assert MODULE.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert not production.exists()
    assert report["verdict"] == "synthetic_pass_production_unverified"
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "not_run"
    assert report["optional_live_providers"]["status"] == "not_run"
    assert all(item["status"] == "not_run" for item in report["manual_desktop"].values())
    assert all(item["status"] == "pass" for item in report["checks"])
    assert {
        "scan.resumable_and_complete",
        "source.original_rows_immutable_after_scan",
        "ui.index_first_read_scope",
        "review.explicit_overlay_only",
        "backup.v15_merge_preserves_approved_audit",
        "correction.reversal_preserves_source_and_other_sessions",
    }.issubset({item["id"] for item in report["checks"]})
    serialized = output.read_text(encoding="utf-8")
    assert "synthetic-1" not in serialized
    assert "https://example.test" not in serialized
    assert "api_key" not in serialized
    assert "price_rows" in serialized


def test_readonly_opt_in_existing_database_remains_unchanged(tmp_path):
    existing = tmp_path / "existing.sqlite3"
    MODULE._seed(existing, MODULE._sessions(40))
    with sqlite3.connect(existing) as conn:
        before = (
            conn.execute("PRAGMA user_version").fetchone()[0],
            tuple(conn.execute("SELECT * FROM daily_prices ORDER BY series_id,session_date")),
            tuple(conn.execute("SELECT * FROM index_daily_closes ORDER BY index_code,session_date")),
        )
    output = tmp_path / "inspect.json"
    assert MODULE.main(["--output", str(output), "--inspect-db", str(existing)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["read_only_local_inspection"]["inspection_only"] is True
    assert report["read_only_local_inspection"]["price_samples_exported"] is False
    assert report["read_only_local_inspection"]["counts"]["daily_prices"] == 80
    assert report["large_production_performance"]["status"] == "not_run"
    with sqlite3.connect(existing) as conn:
        after = (
            conn.execute("PRAGMA user_version").fetchone()[0],
            tuple(conn.execute("SELECT * FROM daily_prices ORDER BY series_id,session_date")),
            tuple(conn.execute("SELECT * FROM index_daily_closes ORDER BY index_code,session_date")),
        )
    assert after == before
    assert "https://example.test" not in output.read_text()


def test_harness_never_overwrites_user_files_or_database_journals(tmp_path):
    user_data = tmp_path / "market.sqlite3"
    user_data.write_bytes(b"untouchable")
    with pytest.raises(SystemExit):
        MODULE.main(["--output", str(user_data), "--inspect-db", str(user_data)])
    with pytest.raises(SystemExit):
        MODULE.main([
            "--output", str(Path(str(user_data) + "-wal")), "--inspect-db", str(user_data)
        ])
    existing_report = tmp_path / "report.json"
    existing_report.write_text("DO NOT OVERWRITE", encoding="utf-8")
    with pytest.raises(SystemExit):
        MODULE.main(["--output", str(existing_report)])
    assert existing_report.read_text(encoding="utf-8") == "DO NOT OVERWRITE"
    assert user_data.read_bytes() == b"untouchable"


def test_invalid_size_is_rejected_before_creating_any_report(tmp_path):
    output = tmp_path / "invalid.json"
    for count in ("0", "31", "20001"):
        with pytest.raises(SystemExit):
            MODULE.main(["--output", str(output), "--sessions", count])
        assert not output.exists()


def test_synthetic_failure_must_not_be_reported_as_production_success():
    report = MODULE._report(
        [MODULE._check("data.integrity", False, {"reason": "synthetic regression"})],
        {"sessions": 128},
        None,
    )
    assert report["synthetic"]["status"] == "fail"
    assert report["verdict"] == "synthetic_failure"
    assert report["manual_desktop"]["macos"]["status"] == "not_run"
