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
    assert all(c["status"] == "pass" for c in report["synthetic"]["checks"])
    assert report["read_only_local_inspection"]["status"] == "not_run"
    assert report["read_only_local_inspection"]["checks"] == []
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
            tuple(
                conn.execute("SELECT * FROM index_daily_closes ORDER BY index_code,session_date")
            ),
        )
    output = tmp_path / "inspect.json"
    assert MODULE.main(["--output", str(output), "--inspect-db", str(existing)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "pass"
    assert report["read_only_local_inspection"]["schema_compatibility"]["status"] == "compatible"
    assert report["read_only_local_inspection"]["schema_compatibility"]["missing_columns"] == {}
    assert all(c["status"] == "pass" for c in report["read_only_local_inspection"]["checks"])
    assert report["read_only_local_inspection"]["inspection_only"] is True
    assert report["read_only_local_inspection"]["price_samples_exported"] is False
    assert report["read_only_local_inspection"]["counts"]["daily_prices"] == 80
    assert report["large_production_performance"]["status"] == "not_run"
    with sqlite3.connect(existing) as conn:
        after = (
            conn.execute("PRAGMA user_version").fetchone()[0],
            tuple(conn.execute("SELECT * FROM daily_prices ORDER BY series_id,session_date")),
            tuple(
                conn.execute("SELECT * FROM index_daily_closes ORDER BY index_code,session_date")
            ),
        )
    assert after == before
    assert "https://example.test" not in output.read_text()


def test_harness_never_overwrites_user_files_or_database_journals(tmp_path):
    user_data = tmp_path / "market.sqlite3"
    user_data.write_bytes(b"untouchable")
    with pytest.raises(SystemExit):
        MODULE.main(["--output", str(user_data), "--inspect-db", str(user_data)])
    with pytest.raises(SystemExit):
        MODULE.main(
            ["--output", str(Path(str(user_data) + "-wal")), "--inspect-db", str(user_data)]
        )
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


def test_v14_copy_reports_migration_required_without_mutation(tmp_path, monkeypatch, capsys):
    """Legacy quality tables can lack superseded_at; inspection must not crash or migrate."""
    legacy = tmp_path / "legacy-v14.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("PRAGMA user_version = 14")
        db.execute("CREATE TABLE daily_prices (session_date TEXT, close TEXT)")
        db.execute("CREATE TABLE index_daily_closes (session_date TEXT, close TEXT)")
        db.execute("CREATE TABLE data_quality_findings (id TEXT PRIMARY KEY, code TEXT)")
        db.execute("CREATE TABLE data_quality_reviews (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO daily_prices VALUES ('2025-01-02', '100')")
    monkeypatch.setattr(
        MODULE,
        "_synthetic",
        lambda *_args: ([MODULE._check("synthetic.fixture", True, {})], {"sessions": 128}),
    )
    report_path = tmp_path / "v14-inspection.json"
    assert MODULE.main(["--inspect-db", str(legacy), "--output", str(report_path)]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "fail"
    assert report["verdict"] == "local_inspection_failed"
    compatibility = report["read_only_local_inspection"]["schema_compatibility"]
    assert compatibility["status"] == "migration_required"
    assert compatibility["actual_version"] == 14
    assert compatibility["expected_version"] == 15
    assert "quality_corrections" in compatibility["missing_tables"]
    assert "superseded_at" in compatibility["missing_columns"]["data_quality_findings"]
    assert "separate backup copy" in compatibility["guidance"]
    assert (
        next(
            c
            for c in report["read_only_local_inspection"]["checks"]
            if c["id"] == "local.index_scoped_query"
        )["status"]
        == "fail"
    )
    assert "local inspection: fail" in capsys.readouterr().out
    with sqlite3.connect(legacy) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
        assert db.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 1
        assert "quality_corrections" not in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_v15_missing_correction_table_is_local_failure_not_synthetic(tmp_path, monkeypatch):
    incomplete = tmp_path / "incomplete-v15.sqlite3"
    MODULE._seed(incomplete, MODULE._sessions(40))
    with sqlite3.connect(incomplete) as db:
        db.execute("DROP TABLE quality_correction_events")
    monkeypatch.setattr(
        MODULE,
        "_synthetic",
        lambda *_args: ([MODULE._check("synthetic.fixture", True, {})], {"sessions": 128}),
    )
    report_path = tmp_path / "incomplete-inspection.json"
    assert MODULE.main(["--inspect-db", str(incomplete), "--output", str(report_path)]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "fail"
    assert report["verdict"] == "local_inspection_failed"
    compatibility = report["read_only_local_inspection"]["schema_compatibility"]
    assert compatibility["status"] == "schema_incomplete"
    assert compatibility["actual_version"] == 15
    assert "quality_correction_events" in compatibility["missing_tables"]
    assert "Never force the version marker" in compatibility["guidance"]
    with sqlite3.connect(incomplete) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        assert db.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 80


def test_synthetic_failure_keeps_passing_local_inspection_distinct():
    report = MODULE._report(
        [MODULE._check("synthetic.issue", False, {})],
        {"sessions": 128},
        [MODULE._check("local.read_only_schema", True, {})],
        {"large_dataset_observed": False, "inspection_only": True},
    )
    assert report["synthetic"]["status"] == "fail"
    assert report["read_only_local_inspection"]["status"] == "pass"
    assert report["verdict"] == "synthetic_failure"
    assert len(report["checks"]) == 2


def _create_v15_inspection_fixture(path, *, missing_table, missing_column):
    """Create a disposable structural fixture, intentionally missing one column."""
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 15")
        for table, columns in MODULE.INSPECTION_REQUIRED_COLUMNS.items():
            # This test checks acceptance diagnostics, not price validation or DDL constraints.
            fields = (
                sorted(columns - {missing_column}) if table == missing_table else sorted(columns)
            )
            schema = ", ".join(f"{column} TEXT" for column in fields)
            db.execute(f"CREATE TABLE {table} ({schema})")
        db.execute("INSERT INTO daily_prices (close) VALUES ('unchanged-test-row')")


@pytest.mark.parametrize(
    ("missing_table", "missing_column"),
    (
        ("data_quality_findings", "id"),
        ("data_quality_findings", "code"),
        ("data_quality_findings", "superseded_at"),
        ("quality_corrections", "state"),
        ("quality_correction_events", "to_state"),
        ("corporate_action_evidence", "status"),
        ("data_quality_reviews", "finding_id"),
        ("daily_prices", "volume"),
        ("index_daily_closes", "source_url"),
        ("data_quality_scan_runs", "run_key"),
        ("data_quality_scan_progress", "cursor_next"),
        ("data_quality_source_state", "source_version"),
    ),
)
def test_v15_missing_required_column_is_not_compatible(
    tmp_path, monkeypatch, missing_table, missing_column
):
    """A v15 marker and present table names must not hide broken data contracts."""
    incomplete = tmp_path / f"v15-no-{missing_table}-{missing_column}.sqlite3"
    _create_v15_inspection_fixture(
        incomplete, missing_table=missing_table, missing_column=missing_column
    )
    monkeypatch.setattr(
        MODULE,
        "_synthetic",
        lambda *_args: ([MODULE._check("synthetic.fixture", True, {})], {"sessions": 128}),
    )
    output = tmp_path / "incomplete-columns.json"
    assert MODULE.main(["--inspect-db", str(incomplete), "--output", str(output)]) == 1

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "fail"
    assert report["verdict"] == "local_inspection_failed"
    compatibility = report["read_only_local_inspection"]["schema_compatibility"]
    assert compatibility["status"] == "schema_incomplete"
    assert compatibility["actual_version"] == 15
    assert compatibility["missing_tables"] == []
    assert compatibility["missing_columns"] == {missing_table: [missing_column]}
    assert "tables or columns" in compatibility["guidance"]
    checks = {check["id"]: check for check in report["read_only_local_inspection"]["checks"]}
    assert checks["local.read_only_schema"]["status"] == "fail"
    assert checks["local.read_only_schema"]["evidence"]["missing_columns"] == {
        missing_table: [missing_column]
    }
    if missing_table == "data_quality_findings":
        assert checks["local.index_scoped_query"]["status"] == "fail"
        assert checks["local.index_scoped_query"]["evidence"]["query_columns_present"] is False
    else:
        assert checks["local.index_scoped_query"]["status"] == "pass"
        assert checks["local.index_scoped_query"]["evidence"]["query_columns_present"] is True
    with sqlite3.connect(incomplete) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        assert db.execute("SELECT close FROM daily_prices").fetchone()[0] == "unchanged-test-row"


@pytest.mark.parametrize(
    "missing_table",
    ("data_quality_scan_runs", "data_quality_scan_progress", "data_quality_source_state"),
)
def test_v15_missing_quality_scan_table_is_incompatible(tmp_path, monkeypatch, missing_table):
    """A migrated v15 copy needs scan, resume and source-revision metadata."""
    existing = tmp_path / "missing-scan-table.sqlite3"
    MODULE._seed(existing, MODULE._sessions(40))
    with sqlite3.connect(existing) as db:
        db.execute(f"DROP TABLE {missing_table}")
    monkeypatch.setattr(
        MODULE,
        "_synthetic",
        lambda *_args: ([MODULE._check("synthetic.fixture", True, {})], {"sessions": 128}),
    )
    output = tmp_path / "missing-scan-table.json"
    assert MODULE.main(["--inspect-db", str(existing), "--output", str(output)]) == 1

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["synthetic"]["status"] == "pass"
    assert report["read_only_local_inspection"]["status"] == "fail"
    assert report["verdict"] == "local_inspection_failed"
    compatibility = report["read_only_local_inspection"]["schema_compatibility"]
    assert compatibility["status"] == "schema_incomplete"
    assert compatibility["actual_version"] == 15
    assert compatibility["missing_tables"] == [missing_table]
    assert compatibility["missing_columns"] == {}
    with sqlite3.connect(existing) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        assert db.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 80
        assert missing_table not in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
