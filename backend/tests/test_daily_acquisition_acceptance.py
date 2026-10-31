"""Disposable issue #26 acceptance harness regressions."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
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

    assert report["verdict"] == "synthetic_pass"
    assert report["synthetic_acceptance"]["verdict"] == "pass"
    assert report["production_acceptance"]["verdict"] == "not_run"
    assert report["run"]["automatic_daily_acquisition_enabled"] is False
    assert report["run"]["credentials_logged"] is False
    assert all(check["status"] == "pass" for check in report["checks"])
    assert all(item["status"] == "not_run" for item in report["acceptance_matrix"])
    assert disposable.is_file()
    assert not normal.exists()


def test_harness_refuses_existing_database(tmp_path):
    database = tmp_path / "existing.sqlite3"
    database.write_bytes(b"not a database")

    assert module.main(["--db-path", str(database)]) == 1


def test_live_mode_uses_a_clean_database_separate_from_synthetic(monkeypatch, tmp_path):
    live_database = tmp_path / "live.sqlite3"
    output = tmp_path / "live-report.json"
    monkeypatch.setattr(module, "_configured", lambda _provider: False)

    assert (
        module.main(
            [
                "--mode",
                "live",
                "--provider",
                "tiingo",
                "--db-path",
                str(live_database),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["run"]["synthetic_database"] != report["run"]["live_database"]
    assert report["production_acceptance"]["verdict"] == "review"
    assert (
        next(item for item in report["checks"] if item["id"] == "live-database-start-empty")[
            "status"
        ]
        == "pass"
    )
    with sqlite3.connect(live_database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
            == 0
        )


def test_partial_provider_matrix_marks_omitted_checks_not_run(tmp_path):
    results = {"tiingo": {"status": "pass", "summary": "ok", "evidence": {"provider": "tiingo"}}}
    matrix = module._acceptance_matrix(results)
    statuses = {item["id"]: item["status"] for item in matrix}

    assert statuses["provider.tiingo"] == "pass"
    assert statuses["provider.alpaca_sip"] == "not_run"
    assert statuses["provider.alpha_vantage"] == "not_run"
    assert statuses["provider.fred"] == "not_run"
    assert all(
        statuses["desktop." + platform] == "not_run" for platform in module.DESKTOP_PLATFORMS
    )


def test_production_report_cannot_pass_without_desktop_evidence(tmp_path):
    results = {
        provider: {"status": "pass", "summary": "ok", "evidence": {}}
        for provider in (*module.EQUITY_PROVIDERS, "fred")
    }
    report = module._report(
        "live",
        tmp_path / "synthetic.sqlite3",
        tmp_path / "live.sqlite3",
        [],
        [module._check("live-check", "pass", "ok")],
        results,
        datetime.now(UTC),
    )

    assert report["production_acceptance"]["verdict"] == "review"
    assert report["verdict"] == "review"
    assert any(
        item["category"] == "packaged_desktop" and item["status"] == "not_run"
        for item in report["acceptance_matrix"]
    )


def test_manual_desktop_evidence_can_complete_production_matrix(tmp_path):
    results = {
        provider: {"status": "pass", "summary": "ok", "evidence": {}}
        for provider in (*module.EQUITY_PROVIDERS, "fred")
    }
    desktop = {
        platform: {
            "status": "pass",
            "summary": f"Clean installed {platform} acceptance passed.",
            "evidence": {
                "tested_at": "2026-10-31T15:30:00Z",
                "build_id": "finintel-2bbdd62",
                "tested_features": ["sidecar readiness", "Action History"],
            },
        }
        for platform in module.DESKTOP_PLATFORMS
    }

    report = module._report(
        "live",
        tmp_path / "synthetic.sqlite3",
        tmp_path / "live.sqlite3",
        [],
        [module._check("live-check", "pass", "ok")],
        results,
        datetime.now(UTC),
        desktop,
    )

    assert report["production_acceptance"]["verdict"] == "pass"
    assert report["verdict"] == "pass"
    assert all(item["status"] == "pass" for item in report["acceptance_matrix"])


def test_record_desktop_evidence_updates_existing_live_report(tmp_path):
    results = {
        provider: {"status": "pass", "summary": "ok", "evidence": {}}
        for provider in (*module.EQUITY_PROVIDERS, "fred")
    }
    report = module._report(
        "live",
        tmp_path / "synthetic.sqlite3",
        tmp_path / "live.sqlite3",
        [],
        [module._check("live-check", "pass", "ok")],
        results,
        datetime.now(UTC),
    )
    report_path = tmp_path / "acceptance-report.json"
    evidence_path = tmp_path / "desktop-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                platform: {
                    "status": "pass",
                    "summary": f"{platform} verified",
                    "evidence": {
                        "tested_at": "2026-10-31T15:30:00Z",
                        "build_id": "finintel-2bbdd62",
                        "tested_features": ["clean install", "Action History"],
                    },
                }
                for platform in module.DESKTOP_PLATFORMS
            }
        ),
        encoding="utf-8",
    )

    assert (
        module.main(
            [
                "--record-desktop-evidence",
                str(report_path),
                "--desktop-evidence",
                str(evidence_path),
            ]
        )
        == 0
    )
    updated = json.loads(report_path.read_text(encoding="utf-8"))
    assert updated["verdict"] == "pass"
    assert updated["production_acceptance"]["verdict"] == "pass"
    assert all(item["status"] == "pass" for item in updated["acceptance_matrix"])
    assert updated["run"]["desktop_evidence_recorded_at"]


@pytest.mark.parametrize(
    "details",
    [
        {},
        {
            "tested_at": "2026-10-31T15:30:00Z",
            "build_id": "finintel-2bbdd62",
            "tested_features": [],
        },
        {
            "tested_at": "not-a-timestamp",
            "build_id": "finintel-2bbdd62",
            "tested_features": ["Action History"],
        },
    ],
)
def test_desktop_pass_requires_complete_evidence(tmp_path, details):
    evidence_path = tmp_path / "desktop-evidence.json"
    evidence_path.write_text(
        json.dumps({"macos": {"status": "pass", "evidence": details}}),
        encoding="utf-8",
    )

    with pytest.raises((TypeError, ValueError)):
        module._load_desktop_evidence(evidence_path)


def test_extensionless_database_paths_protect_all_sqlite_sidecars(monkeypatch, tmp_path):
    database = tmp_path / "marketdata"
    monkeypatch.setenv("FININTEL_DB_PATH", str(database))
    sidecars = {Path(str(database) + suffix) for suffix in module.SQLITE_SIDECAR_SUFFIXES}

    assert sidecars.issubset(module._known_database_paths())
    for sidecar in sidecars:
        sidecar.write_bytes(b"sidecar contents")
        before = sidecar.read_bytes()
        assert module.main(["--output", str(sidecar)]) == 1
        assert sidecar.read_bytes() == before


def test_failed_synthetic_suite_forces_live_overall_failure(tmp_path):
    results = {
        provider: {"status": "pass", "summary": "ok", "evidence": {}}
        for provider in (*module.EQUITY_PROVIDERS, "fred")
    }
    desktop = {
        platform: {
            "status": "pass",
            "summary": "verified",
            "evidence": {
                "tested_at": "2026-10-31T15:30:00Z",
                "build_id": "finintel-2bbdd62",
                "tested_features": ["Action History"],
            },
        }
        for platform in module.DESKTOP_PLATFORMS
    }

    report = module._report(
        "live",
        tmp_path / "synthetic.sqlite3",
        tmp_path / "live.sqlite3",
        [module._check("synthetic-failure", "fail", "broken")],
        [module._check("live-check", "pass", "ok")],
        results,
        datetime.now(UTC),
        desktop,
    )

    assert report["synthetic_acceptance"]["verdict"] == "fail"
    assert report["production_acceptance"]["verdict"] == "pass"
    assert report["verdict"] == "fail"


def test_report_output_cannot_replace_sqlite_file(tmp_path):
    database = tmp_path / "active.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('preserve')")
    before = database.read_bytes()

    assert module.main(["--output", str(database)]) == 1
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "preserve"


@pytest.mark.parametrize(
    "filename",
    ["finintel.sqlite3-wal", "finintel.sqlite3-shm", "finintel.sqlite3-journal"],
)
def test_report_output_cannot_replace_sqlite_sidecar(filename, tmp_path):
    sidecar = tmp_path / filename
    sidecar.write_bytes(b"sqlite sidecar contents")
    before = sidecar.read_bytes()

    assert module.main(["--output", str(sidecar)]) == 1
    assert sidecar.read_bytes() == before


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
