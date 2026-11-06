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
    base = {"start": date(2026, 9, 1), "end": date(2026, 10, 1)}
    return ModelJobRequest(**(base | changes))


def result(source="a" * 64):
    lineage = {
        "model_id": "builtin.sma_crossover", "model_version": "1.0.0",
        "source_snapshot_sha256": source,
        "source_identity": {"code": "SP500", "provider": "fred", "adjustment": "price_index"},
        "requested_start": "2026-09-01", "requested_end": "2026-10-01",
        "parameters": {"fast_window": 5, "slow_window": 20},
        "quality_policy": "require_complete",
    }
    points = [{"session_date": "2026-09-01"}]
    digest = lambda value: hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    return {
        "execution_contract": "model-research-evaluation-v1", "status": "completed",
        "backtest_validated": False, "lineage": lineage,
        "reproducibility_sha256": digest(lineage),
        "output_sha256": digest(points), "points": points,
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
    identifier, selected = claimed
    assert identifier == created["id"] and selected == request()
    assert ledger.set_progress(identifier, 75)
    assert not ledger.set_progress(identifier, 20)
    assert ledger.complete(identifier, result())
    assert not ledger.complete(identifier, result())  # No second completion.
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
    assert not ledger.set_progress(running, 50)
    assert not ledger.complete(running, result())
    assert not ledger.fail(running, "worker_failure")
    assert ledger.finish_cancelled(running)
    assert ledger.get(running)["state"] == "cancelled"
    assert not ledger.finish_cancelled(running)


def test_retry_source_pins_attempt_quota_and_interrupted_restart(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    expected = "a" * 64
    first = ledger.enqueue(request(expected_snapshot_sha256=expected))["id"]
    assert ledger.claim_next()[0] == first
    assert ledger.fail(first, "source_changed")
    assert ledger.retry(first)
    assert ledger.claim_next()[1].expected_snapshot_sha256 == expected
    assert ledger.get(first)["attempts"] == 2
    assert ledger.reconcile_interrupted() == 1
    assert ledger.get(first)["error_code"] == "interrupted"
    assert ledger.retry(first)
    assert ledger.claim_next()[0] == first
    assert ledger.get(first)["attempts"] == MAX_ATTEMPTS
    assert ledger.fail(first, "worker_failure")
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
        ledger.fail(job, "Exception: secret API key")
    with pytest.raises(ModelJobStoreError):
        ledger.complete(job, {**result(), "backtest_validated": True})
    with pytest.raises(ModelJobStoreError):
        ledger.complete(job, result(source="invalid"))
    tampered = result()
    tampered["lineage"]["source_identity"]["code"] = "DJIA"
    import hashlib
    import json
    tampered["reproducibility_sha256"] = hashlib.sha256(json.dumps(
        tampered["lineage"], sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    with pytest.raises(ModelJobStoreError, match="does not match queued request"):
        ledger.complete(job, tampered)
    with pytest.raises(ModelJobStoreError):
        ledger.set_progress(job, 101)
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
        ledger.complete(identifier, result(source="a" * 64))
    assert ledger.get(identifier)["state"] == "running"
    assert ledger.fail(identifier, "source_changed")
