"""Synthetic, standalone durable model-job ledger tests (no private data)."""

import hashlib
import json
from datetime import date

import pytest
from pydantic import ValidationError

from finintel.models.job_store import (
    MAX_ACTIVE_JOBS,
    MAX_ATTEMPTS,
    ModelJobLedger,
    ModelJobRequest,
    ModelJobStoreError,
)


def request(**changes):
    base = {
        "start": date(2026, 9, 1),
        "end": date(2026, 10, 1),
        "expected_snapshot_sha256": "a" * 64,
    }
    return ModelJobRequest(**(base | changes))


def result(source="a" * 64):
    lineage = {
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "implementation_revision": "sma-crossover-observed-close-v1",
        "parameters": {"fast_window": 5, "slow_window": 20},
        "source_identity": {
            "instrument_kind": "index", "code": "SP500", "provider": "fred",
            "adjustment": "price_index", "return_semantics": "price_index",
            "frequency": "daily", "units": "index_points",
        },
        "source_snapshot_sha256": source,
        "analysis_calculation_version": "index-analytics-v2",
        "requested_start": "2026-09-01",
        "requested_end": "2026-10-01",
        "first_observed_session": "2026-09-01",
        "last_observed_session": "2026-09-21",
        "quality_policy": "require_complete",
        "quality_status": "complete",
        "quality_verified": True,
        "point_in_time_membership": "not_used",
        "dividends_included": False,
        "corporate_action_adjustment": "none_index_price_level",
    }
    points = [
        {
            "session_date": f"2026-09-{i:02d}", "close": str(100+i),
            "fast_sma": None, "slow_sma": None, "signal_after_close": None,
        }
        for i in range(1, 22)
    ]
    def digest(value):
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
    return {
        "execution_contract": "model-research-evaluation-v1",
        "status": "completed",
        "backtest_validated": False,
        "persistent_job": False,
        "read_only": True,
        "no_network": True,
        "no_synthetic_sessions": True,
        "signal_after_close_only": True,
        "observations": len(points),
        "evaluated_at_utc": "2026-10-02T00:00:00+00:00",
        "quality_warning": None,
        "lineage": lineage,
        "reproducibility_sha256": digest(lineage),
        "output_sha256": digest(points),
        "points": points,
    }


def test_create_queue_claim_complete_and_read_only_history(tmp_path):
    path = tmp_path / "model-jobs.sqlite3"
    ledger = ModelJobLedger(path)
    assert ledger.list() == {"total": 0, "items": []}
    assert not path.exists()  # Read-only listing never initializes a DB.
    created = ledger.enqueue(request())
    assert created["state"] == "queued" and created["attempts"] == 0
    assert created["request_sha256"] and path.exists()
    claimed = ledger.claim_next()
    assert claimed is not None
    identifier, selected, attempt = claimed
    assert identifier == created["id"] and selected == request()
    assert ledger.set_progress(identifier, 1, 75)
    assert not ledger.set_progress(identifier, 1, 20)
    assert ledger.complete(identifier, 1, result())
    assert not ledger.complete(identifier, 1, result())  # No second completion.
    record = ledger.get(identifier, include_result=True)
    assert record["state"] == "completed" and record["progress"] == 100
    assert record["source_snapshot_sha256"] == "a" * 64
    assert record["result"]["output_sha256"] == record["output_sha256"]
    assert ledger.get(identifier)["output_sha256"] is not None
    assert ledger.list()["total"] == 1
    assert ledger.claim_next() is None


def test_cancel_queued_or_running_is_cooperative_and_blocks_late_completion(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    queued = ledger.enqueue(request())["id"]
    assert ledger.cancel(queued) == "cancelled"
    assert ledger.claim_next() is None
    running = ledger.enqueue(request())["id"]
    assert ledger.claim_next()[0] == running
    assert ledger.cancel(running) == "cancel_requested"
    assert not ledger.set_progress(running, 1, 50)
    assert not ledger.complete(running, 1, result())
    assert not ledger.fail(running, 1, "worker_failure")
    assert ledger.finish_cancelled(running, 1)
    assert ledger.get(running)["state"] == "cancelled"
    assert not ledger.finish_cancelled(running, 1)


def test_retry_source_pins_attempt_quota_and_interrupted_restart(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    expected = "a" * 64
    first = ledger.enqueue(request(expected_snapshot_sha256=expected))["id"]
    assert ledger.claim_next()[0] == first
    assert ledger.fail(first, 1, "source_changed")
    assert ledger.retry(first)
    assert ledger.claim_next()[1].expected_snapshot_sha256 == expected
    assert ledger.get(first)["attempts"] == 2
    assert ledger.reconcile_interrupted() == 1
    assert ledger.get(first)["error_code"] == "interrupted"
    assert ledger.retry(first)
    assert ledger.claim_next()[0] == first
    assert ledger.get(first)["attempts"] == MAX_ATTEMPTS
    assert ledger.fail(first, 3, "worker_failure")
    assert not ledger.retry(first)
    assert ledger.reconcile_interrupted() == 0


def test_restart_cancel_intent_remains_cancelled(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    job = ledger.enqueue(request())["id"]
    ledger.claim_next()
    ledger.cancel(job)
    assert ModelJobLedger(tmp_path / "jobs.sqlite").reconcile_interrupted() == 1
    assert ledger.get(job)["state"] == "cancelled"


def test_reject_unallowlisted_code_paths_and_raw_exception_details(tmp_path):
    with pytest.raises(ValidationError):
        request(module_path="/private/models.py")
    with pytest.raises(ValidationError):
        request(code="VIX")
    with pytest.raises(ValidationError):
        request(model_version="2.0.0")
    with pytest.raises(ValidationError):
        request(start=date(2099, 12, 1), end=date(2099, 12, 2))
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    with pytest.raises(ModelJobStoreError):
        ledger.enqueue({"start": "2026-09-01"})
    job = ledger.enqueue(request())["id"]
    ledger.claim_next()
    with pytest.raises(ModelJobStoreError):
        ledger.fail(job, 1, "Exception: secret API key")
    with pytest.raises(ModelJobStoreError):
        ledger.complete(job, 1, {**result(), "backtest_validated": True})
    with pytest.raises(ModelJobStoreError):
        ledger.complete(job, 1, result(source="invalid"))
    tampered = result()
    tampered["lineage"]["source_identity"]["code"] = "DJIA"
    tampered["reproducibility_sha256"] = hashlib.sha256(
        json.dumps(
            tampered["lineage"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ModelJobStoreError, match="does not match queued request"):
        ledger.complete(job, 1, tampered)
    with pytest.raises(ModelJobStoreError):
        ledger.set_progress(job, 1, 101)
    with pytest.raises(ModelJobStoreError):
        ledger.list(limit=101)


def test_unsafe_path_and_preexisting_unknown_schema_are_refused(tmp_path, monkeypatch):
    path = tmp_path / "jobs.sqlite"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    with pytest.raises(ModelJobStoreError, match="Unsafe"):
        ModelJobLedger(path).enqueue(request())
    monkeypatch.delenv("FININTEL_DB_PATH")
    # A symlink cannot redirect the ledger to an existing file.
    target = tmp_path / "target.sqlite"
    target.write_text("sentinel")
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(target)
    with pytest.raises(ModelJobStoreError, match="Unsafe"):
        ModelJobLedger(alias).enqueue(request())
    assert target.read_text() == "sentinel"
    import sqlite3

    with sqlite3.connect(tmp_path / "foreign.sqlite") as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ModelJobStoreError):
        ModelJobLedger(tmp_path / "foreign.sqlite").enqueue(request())


def test_queue_quota_with_no_implicit_eviction(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    for _ in range(MAX_ACTIVE_JOBS):
        ledger.enqueue(request())
    with pytest.raises(ModelJobStoreError, match="queue is full"):
        ledger.enqueue(request())
    assert ledger.list(limit=MAX_ACTIVE_JOBS)["total"] == MAX_ACTIVE_JOBS
    assert ledger.cancel(ledger.list()["items"][0]["id"]) == "cancelled"
    assert ledger.enqueue(request())["state"] == "queued"


def test_two_ledger_connections_cannot_claim_the_same_job(tmp_path):
    path = tmp_path / "shared.sqlite"
    first = ModelJobLedger(path)
    second = ModelJobLedger(path)
    identifier = first.enqueue(request())["id"]
    assert first.claim_next()[0] == identifier
    assert second.claim_next() is None


def test_result_cannot_bypass_expected_source_revision(tmp_path):
    ledger = ModelJobLedger(tmp_path / "pinned.sqlite")
    identifier = ledger.enqueue(request(expected_snapshot_sha256="c" * 64))["id"]
    ledger.claim_next()
    with pytest.raises(ModelJobStoreError, match="does not match queued request"):
        ledger.complete(identifier, 1, result(source="a" * 64))
    assert ledger.get(identifier)["state"] == "running"
    assert ledger.fail(identifier, 1, "source_changed")



def test_default_and_relative_job_paths_allow_read_only_queries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FININTEL_DB_PATH", "data/finintel.sqlite3")
    monkeypatch.delenv("FININTEL_MODEL_JOBS_DB_PATH", raising=False)
    default = ModelJobLedger()
    created = default.enqueue(request())
    assert default.get(created["id"])["id"] == created["id"]
    assert default.list()["total"] == 1
    monkeypatch.setenv("FININTEL_MODEL_JOBS_DB_PATH", "custom/ledger.sqlite3")
    relative = ModelJobLedger()
    created = relative.enqueue(request())
    assert relative.get(created["id"])["state"] == "queued"
    assert relative.list()["total"] == 1


def test_stale_attempt_cannot_change_new_claim_or_recovery_state(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    identifier = ledger.enqueue(request())["id"]
    old_id, _, first_attempt = ledger.claim_next()
    assert old_id == identifier and first_attempt == 1
    assert ledger.cancel(identifier) == "cancel_requested"
    assert ledger.finish_cancelled(identifier, first_attempt)
    assert ledger.retry(identifier)
    _, _, second_attempt = ledger.claim_next()
    assert second_attempt == 2
    assert not ledger.set_progress(identifier, first_attempt, 80)
    assert not ledger.complete(identifier, first_attempt, result())
    assert not ledger.fail(identifier, first_attempt, "worker_failure")
    assert not ledger.finish_cancelled(identifier, first_attempt)
    assert ledger.get(identifier)["state"] == "running"
    assert ledger.set_progress(identifier, second_attempt, 55)
    assert ledger.reconcile_interrupted() == 1
    assert ledger.retry(identifier)
    _, _, third_attempt = ledger.claim_next()
    assert third_attempt == 3
    assert not ledger.complete(identifier, second_attempt, result())
    assert not ledger.set_progress(identifier, second_attempt, 80)
    assert not ledger.fail(identifier, second_attempt, "worker_failure")
    assert ledger.complete(identifier, third_attempt, result())
    assert ledger.get(identifier)["state"] == "completed"
    with pytest.raises(ModelJobStoreError, match="Invalid model-job attempt"):
        ledger.complete(identifier, True, result())


def test_durable_jobs_must_be_source_pinned_from_enqueue(tmp_path):
    with pytest.raises(ValidationError):
        ModelJobRequest(start=date(2026, 9, 1), end=date(2026, 10, 1))
    with pytest.raises(ValidationError):
        request(expected_snapshot_sha256=None)
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    first = ledger.enqueue(request(expected_snapshot_sha256="a" * 64))["id"]
    assert ledger.claim_next()[1].expected_snapshot_sha256 == "a" * 64
    assert ledger.fail(first, 1, "source_changed")
    assert ledger.retry(first)
    retry_claim = ledger.claim_next()
    assert retry_claim[1].expected_snapshot_sha256 == "a" * 64
    assert not ledger.complete(first, 1, result(source="a" * 64))
    with pytest.raises(ModelJobStoreError, match="does not match queued request"):
        ledger.complete(first, retry_claim[2], result(source="b" * 64))
    assert ledger.get(first)["state"] == "running"


def test_result_schema_rejects_extra_fields_at_all_levels(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    job = ledger.enqueue(request())["id"]
    attempt = ledger.claim_next()[2]
    leaked = result()
    leaked["api_token"] = "secret-do-not-retain"
    with pytest.raises(ModelJobStoreError, match="output schema"):
        ledger.complete(job, attempt, leaked)
    leaked = result()
    leaked["lineage"]["untrusted_model_source"] = "private code"
    with pytest.raises(ModelJobStoreError, match="lineage schema"):
        ledger.complete(job, attempt, leaked)
    leaked = result()
    leaked["points"][0]["notes"] = "private context"
    with pytest.raises(ModelJobStoreError, match="output schema"):
        ledger.complete(job, attempt, leaked)
    assert ledger.get(job, include_result=True)["result"] is None
    assert ledger.complete(job, attempt, result())
    saved = ledger.get(job, include_result=True)["result"]
    assert "api_token" not in saved and "untrusted_model_source" not in saved["lineage"]
