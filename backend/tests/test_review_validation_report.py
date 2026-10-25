"""Offline acceptance report verdict regression tests."""

import importlib.util
from pathlib import Path

script = Path(__file__).resolve().parents[2] / "scripts" / "review_validation_report.py"
spec = importlib.util.spec_from_file_location("review_validation_report", script)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    return {
        "integrity": {"issues": {}},
        "coverage": {"xnys_internal_gap_candidates": 0},
        "corporate": {"corporate_action_jump_candidate_count": 0},
        "provider": {
            "cross_provider_pairs_compared": 4,
            "cross_provider_close_divergences_over_1pct": 0,
        },
        "performance": {"all_acceptance_targets_met": True},
        "corporate_reference": {"events": []},
    }


def test_clean_report_passes():
    assert module.assess(fixture())["verdict"] == "pass"


def test_missing_sections_fail():
    assert module.assess({})["verdict"] == "fail"


def test_existing_gap_candidates_need_review():
    report = fixture()
    report["coverage"]["xnys_internal_gap_candidates"] = 257282
    assert module.assess(report)["verdict"] == "review"


def test_price_issues_or_slow_queries_fail():
    report = fixture()
    report["integrity"]["issues"] = {"bad_ohlc": 1}
    assert module.assess(report)["verdict"] == "fail"
    report = fixture()
    report["performance"]["all_acceptance_targets_met"] = False
    assert module.assess(report)["verdict"] == "fail"
