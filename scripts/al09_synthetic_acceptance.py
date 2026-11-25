"""Run the mandatory AL-09 synthetic Python checks and emit allowlisted evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_CASES = (
    "test_real_child_executes_each_approved_analysis_model",
    "test_durable_backtest_uses_frozen_source_and_persists_exact_engine_report",
    "test_al09_maximum_observed_window",
    "test_al09_maximum_backtest_source_window",
    "test_al09_market_restore_preserves_separate_completed_ledger",
    "test_worker_recovers_after_transient_claim_failure",
    "test_supervisor_cancels_child_without_orphan_or_late_result",
    "test_worker_environment_never_passes_provider_keys",
    "test_managed_job_api_rejects_missing_token_and_dynamic_modules",
    "test_analysis_api_authenticates_every_route_before_validation",
    "test_backtest_authenticates_each_operation_before_processing",
)
TARGETS = (
    "test_analysis_model_contracts.py",
    "test_risk_model.py",
    "test_regime_model.py",
    "test_backtest_contract.py",
    "test_model_job_store.py",
    "test_database_backup.py",
    "test_quality.py::test_failed_quality_migration_rolls_back_and_retry_succeeds",
    *(f"test_model_worker.py::{case}" for case in WORKER_CASES),
)


class Evidence:
    def __init__(self) -> None:
        self.selected: set[str] = set()
        self.results: dict[str, str] = {}

    def pytest_collection_finish(self, session) -> None:
        self.selected = {item.nodeid for item in session.items}

    def pytest_runtest_logreport(self, report) -> None:
        if report.failed:
            self.results[report.nodeid] = "FAIL"
        elif report.skipped:
            if self.results.get(report.nodeid) != "FAIL":
                self.results[report.nodeid] = "SKIPPED"
        elif report.when == "call":
            self.results.setdefault(report.nodeid, "PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    evidence = Evidence()
    started = datetime.now(UTC)
    blocker = None
    commit = None
    exit_code = None
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        blocker = "git_commit_unavailable"
    if blocker is None:
        try:
            import pytest
        except ImportError:
            blocker = "pytest_unavailable"
        else:
            # Explicit targets prevent a missing mandatory module or test from silently passing.
            os.environ.pop("PYTEST_ADDOPTS", None)
            exit_code = int(
                pytest.main(
                    [
                        "-q",
                        "--rootdir",
                        str(ROOT),
                        *(f"backend/tests/{target}" for target in TARGETS),
                    ],
                    plugins=[evidence],
                )
            )
    checks = {
        status: sorted(
            node for node, outcome in evidence.results.items() if outcome == status
        )
        for status in ("PASS", "FAIL", "SKIPPED")
    }
    not_run = sorted(evidence.selected - evidence.results.keys())
    if blocker:
        status = "BLOCKED"
    elif exit_code != 0 or checks["FAIL"]:
        status = "FAIL"
    elif not evidence.selected or checks["SKIPPED"] or not_run:
        status = "BLOCKED"
        blocker = "mandatory_checks_incomplete"
    else:
        status = "PASS"
    report = {
        "contract_version": "al09-synthetic-acceptance-v1",
        "git_commit": commit,
        "platform": {
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
        },
        "suite": "AL-09 mandatory synthetic Python acceptance",
        "started_at_utc": started.isoformat(),
        "validated_at_utc": datetime.now(UTC).isoformat(),
        "targets": list(TARGETS),
        "executed_checks": sorted(evidence.results.keys() - set(checks["SKIPPED"])),
        "passed_checks": checks["PASS"],
        "failed_checks": checks["FAIL"],
        "skipped_checks": checks["SKIPPED"],
        "not_run_checks": not_run,
        "pytest_exit_code": exit_code,
        "blocker": blocker,
        "overall_automated_status": status,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    # Resolve pytest's explicit paths from the same checkout used for the commit.
    os.chdir(ROOT)
    sys.exit(main())
