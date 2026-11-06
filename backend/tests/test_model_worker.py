"""Phase 2B1 synthetic process, cancellation, and managed API acceptance tests."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.models import worker
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.job_store import ModelJobLedger, ModelJobRequest, ModelJobStoreError
from finintel.storage.db import _write_connection

START = date(2026, 9, 1)
END = START + timedelta(days=34)
TOKEN = "issue58-managed-desktop-test-token-value-32-bytes"


def _market(path):
    conn = _write_connection(path)
    with conn:
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES ('SP500','fred',?,?,'2026-10-25T00:00:00+00:00','local-test')",
            [((START + timedelta(days=i)).isoformat(), str(100 + i)) for i in range(35)],
        )
    conn.close()


def _setup(tmp_path, monkeypatch):
    price = tmp_path / "prices.sqlite3"
    jobs = tmp_path / "jobs.sqlite3"
    _market(price)
    monkeypatch.setenv("FININTEL_DB_PATH", str(price))
    monkeypatch.setenv("FININTEL_MODEL_JOBS_DB_PATH", str(jobs))
    result = evaluate_builtin_indicator(
        start=START, end=END, quality_policy="allow_unverified", db=price
    )
    request = ModelJobRequest(
        start=START,
        end=END,
        quality_policy="allow_unverified",
        expected_snapshot_sha256=result["lineage"]["source_snapshot_sha256"],
    )
    return price, ModelJobLedger(jobs), request


def _await_state(ledger, identifier, states, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = ledger.get(identifier)
        if record and record["state"] in states:
            return record
        time.sleep(0.05)
    raise AssertionError("Local model job did not reach a terminal state")


def test_managed_fixed_routes_validate_queue_pagination_cancel_retry(tmp_path, monkeypatch):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    payload = request.model_dump(mode="json")
    client = TestClient(app)  # no lifespan: deterministic queue API without a live consumer
    headers = {"x-finintel-process-token": TOKEN}
    assert client.post("/api/v1/models/jobs", json=payload).status_code == 403
    assert client.get("/api/v1/models/jobs").status_code == 403
    assert client.post("/api/v1/models/jobs", json=payload, headers=headers).status_code == 201
    for _ in range(10):
        assert client.post("/api/v1/models/jobs", json=payload, headers=headers).status_code == 201
    page1 = client.get("/api/v1/models/jobs", headers=headers).json()
    page2 = client.get("/api/v1/models/jobs?limit=10&offset=10", headers=headers).json()
    assert page1["total"] == 11 and len(page1["items"]) == 10
    assert len(page2["items"]) == 1
    identifier = page1["items"][0]["id"]
    assert page1["items"][0]["status"] == "queued"
    assert client.get(f"/api/v1/models/jobs/{identifier}", headers=headers).status_code == 200
    assert (
        client.get(f"/api/v1/models/jobs/{identifier}/result", headers=headers).status_code == 409
    )
    assert (
        client.post(f"/api/v1/models/jobs/{identifier}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )
    assert (
        client.post(f"/api/v1/models/jobs/{identifier}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )
    assert (
        client.post(f"/api/v1/models/jobs/{identifier}/retry", headers=headers).json()["status"]
        == "queued"
    )
    assert (
        client.post(f"/api/v1/models/jobs/{identifier}/retry", headers=headers).json()["status"]
        == "queued"
    )
    assert ledger.get(identifier)["attempts"] == 0
    bad = client.post(
        "/api/v1/models/jobs",
        json={**payload, "module_path": "/private/secret/model.py"},
        headers=headers,
    )
    assert bad.status_code == 422 and "/private/" not in bad.text
    assert (
        client.post(
            "/api/v1/models/jobs",
            json={**payload, "model_id": "custom.private"},
            headers=headers,
        ).status_code
        == 422
    )
    assert client.get("/api/v1/models/jobs?limit=101", headers=headers).status_code == 422
    assert client.get("/api/v1/models/jobs/../../etc/passwd", headers=headers).status_code in (
        404,
        422,
    )


def test_isolated_child_completes_strict_pinned_result_without_price_writes(tmp_path, monkeypatch):
    price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    before = price.stat().st_size
    supervisor = worker.ModelJobSupervisor(ledger)
    supervisor.start()
    try:
        record = _await_state(ledger, identifier, {"completed", "failed"})
    finally:
        supervisor.stop()
    assert record["state"] == "completed", record
    assert record["attempts"] == 1 and record["status"] == "completed"
    saved = ledger.get(identifier, include_result=True)
    assert saved["result"]["lineage"]["source_snapshot_sha256"] == request.expected_snapshot_sha256
    assert saved["result"]["status"] == "completed"
    assert saved["result"]["backtest_validated"] is False
    assert saved["result"]["no_network"] is True
    assert price.stat().st_size == before
    with sqlite3.connect(price) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_daily_closes").fetchone()[0] == 35


def test_worker_rejects_changed_source_before_claim(tmp_path, monkeypatch):
    price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    with sqlite3.connect(price) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='999' WHERE index_code='SP500' "
            "AND session_date='2026-09-03'"
        )
    supervisor = worker.ModelJobSupervisor(ledger)
    supervisor.start()
    try:
        record = _await_state(ledger, identifier, {"completed", "failed"})
    finally:
        supervisor.stop()
    assert record["state"] == "failed"
    assert record["error_code"] == "source_changed"
    assert ledger.get(identifier, include_result=True)["result"] is None


def test_final_source_pin_gate_rejects_change_before_persistence(tmp_path, monkeypatch):
    price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    original = worker.ModelJobSupervisor._source_is_current
    changed = False

    def mutate_before_store(req):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(price) as conn:
                conn.execute(
                    "UPDATE index_daily_closes SET close='888' WHERE index_code='SP500' "
                    "AND session_date='2026-09-04'"
                )
        return original(req)

    monkeypatch.setattr(
        worker.ModelJobSupervisor, "_source_is_current", staticmethod(mutate_before_store)
    )
    supervisor = worker.ModelJobSupervisor(ledger)
    supervisor.start()
    try:
        record = _await_state(ledger, identifier, {"completed", "failed"})
    finally:
        supervisor.stop()
    assert changed
    assert record["state"] == "failed" and record["error_code"] == "source_changed"
    assert ledger.get(identifier, include_result=True)["result"] is None


def test_restart_reconciles_inflight_without_resuming(tmp_path, monkeypatch):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    assert ledger.claim_next()[2] == 1
    supervisor = worker.ModelJobSupervisor(ledger)
    supervisor.start()
    try:
        record = _await_state(ledger, identifier, {"failed"})
    finally:
        supervisor.stop()
    assert record["error_code"] == "interrupted"
    assert record["attempts"] == 1
    assert ledger.claim_next() is None
    assert ledger.retry(identifier)
    assert ledger.claim_next()[2] == 2
    assert not ledger.fail(identifier, 1, "worker_failure")


class _HangingChild:
    def __init__(self, *_args, **_kwargs):
        self.dead = False
        self.pid = 999999
        self.spawned.set()

    def poll(self):
        return -9 if self.dead else None

    def communicate(self, input=None, timeout=None):
        if self.dead:
            return b"", b""
        time.sleep(min(timeout or 0, 0.01))
        raise subprocess.TimeoutExpired(cmd="synthetic-fixed-child", timeout=timeout)

    def kill(self):
        self.dead = True


def _run_hung_attempt(tmp_path, monkeypatch, *, cancel):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    claim = ledger.claim_next()
    assert claim is not None
    spawned = threading.Event()
    monkeypatch.setattr(_HangingChild, "spawned", spawned, raising=False)
    monkeypatch.setattr(worker.subprocess, "Popen", _HangingChild)
    # Synthetic children do not own a Windows OS process handle.
    monkeypatch.setattr(worker, "_limit_windows_worker", lambda _process: None)
    monkeypatch.setattr(
        worker.ModelJobSupervisor,
        "_terminate",
        staticmethod(lambda process: process.kill() if process else None),
    )
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "CANCEL_GRACE_SECONDS", 0.04)
    monkeypatch.setattr(worker, "WALL_TIMEOUT_SECONDS", 0.09)
    supervisor = worker.ModelJobSupervisor(ledger)
    thread = threading.Thread(target=supervisor._execute, args=claim)
    thread.start()
    assert spawned.wait(timeout=2)
    if cancel:
        assert ledger.cancel(identifier) == "cancel_requested"
        assert ledger.get(identifier)["status"] == "cancel_requested"
    thread.join(timeout=3)
    assert not thread.is_alive()
    return ledger.get(identifier)


def test_running_cancel_has_bounded_hard_stop_and_no_late_result(tmp_path, monkeypatch):
    record = _run_hung_attempt(tmp_path, monkeypatch, cancel=True)
    assert record["state"] == "cancelled"
    assert record["attempts"] == 1
    assert record["output_sha256"] is None


def test_running_timeout_is_safe_resource_failure(tmp_path, monkeypatch):
    record = _run_hung_attempt(tmp_path, monkeypatch, cancel=False)
    assert record["state"] == "failed"
    assert record["error_code"] == "resource_limit"
    assert record["output_sha256"] is None


def test_two_worker_owners_respect_exclusive_lock_and_once_only_claim(tmp_path, monkeypatch):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    first = worker.ModelJobSupervisor(ledger)
    second = worker.ModelJobSupervisor(ModelJobLedger(ledger.path))
    first.start()
    second.start()
    try:
        record = _await_state(ledger, identifier, {"completed", "failed"})
    finally:
        first.stop()
        second.stop()
    assert record["state"] == "completed"
    assert record["attempts"] == 1
    assert ledger.claim_next() is None


def test_errors_are_safe_codes_not_paths_or_credentials():
    assert worker._safe_error(RuntimeError("/secret/token=ABC")) == "worker_failure"
    assert worker._safe_error(MemoryError()) == "resource_limit"
    assert (
        worker._safe_error(
            worker.AnalyticsError("Local source snapshot changed; refresh model inputs")
        )
        == "source_changed"
    )


def test_supervisor_recovers_after_transient_ledger_error(tmp_path, monkeypatch):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    original_claim = ledger.claim_next
    failures = 0

    def flaky_claim():
        nonlocal failures
        failures += 1
        if failures == 1:
            raise ModelJobStoreError("Synthetic transient ledger outage")
        return original_claim()

    monkeypatch.setattr(ledger, "claim_next", flaky_claim)
    supervisor = worker.ModelJobSupervisor(ledger)
    supervisor.start()
    try:
        record = _await_state(ledger, identifier, {"completed", "failed"}, timeout=20)
        assert failures >= 2
        assert record["state"] == "completed", record
        assert record["attempts"] == 1
    finally:
        supervisor.stop()


def test_waiting_supervisor_takes_over_after_first_owner_exits(tmp_path, monkeypatch):
    _price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    waiting = worker.ModelJobSupervisor(ModelJobLedger(ledger.path))
    with worker._exclusive_owner(ledger.path) as owned:
        assert owned
        waiting.start()
        time.sleep(0.4)
        assert ledger.get(identifier)["state"] == "queued"
    try:
        completed = _await_state(ledger, identifier, {"completed", "failed"}, timeout=20)
        assert completed["state"] == "completed", completed
        assert completed["attempts"] == 1
    finally:
        waiting.stop()


def test_worker_environment_excludes_custom_module_paths_and_tokens(tmp_path, monkeypatch):
    _price, ledger, _request = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("PYTHONPATH", "/private/injected/plugins")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    env = worker._child_env(ledger)
    assert "PYTHONPATH" not in env
    assert "OPENAI_API_KEY" not in env
    assert "FININTEL_PROCESS_TOKEN" not in env
    assert env["FININTEL_WORKER_PARENT_PID"] == str(os.getpid())


def test_corrupted_persisted_result_never_reaches_api(tmp_path, monkeypatch):
    price, ledger, request = _setup(tmp_path, monkeypatch)
    identifier = ledger.enqueue(request)["id"]
    attempt = ledger.claim_next()[2]
    evaluated = evaluate_builtin_indicator(
        start=START,
        end=END,
        quality_policy=request.quality_policy,
        expected_snapshot_sha256=request.expected_snapshot_sha256,
        db=price,
    )
    assert ledger.complete(identifier, attempt, evaluated)
    original = ledger.get(identifier, include_result=True)["result"]
    headers = {"x-finintel-process-token": TOKEN}
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    with sqlite3.connect(ledger.path) as conn:
        for location in ("points", "lineage"):
            altered = json.loads(json.dumps(original))
            if location == "points":
                altered["points"][-1]["close"] = "8888"
            else:
                altered["lineage"]["quality_status"] = "fake"
            conn.execute(
                "UPDATE model_jobs SET result_json=? WHERE id=?",
                (json.dumps(altered), identifier),
            )
            conn.commit()
            with pytest.raises(ModelJobStoreError, match="integrity"):
                ledger.get(identifier, include_result=True)
            response = TestClient(app).get(
                f"/api/v1/models/jobs/{identifier}/result", headers=headers
            )
            assert response.status_code == 503
            assert "8888" not in response.text
            assert "fake" not in response.text
        conn.execute(
            "UPDATE model_jobs SET result_json=? WHERE id=?",
            (json.dumps(original), identifier),
        )
    assert ledger.get(identifier, include_result=True)["result"] == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent PID regression")
def test_original_supervisor_pid_survives_frozen_bootloader(monkeypatch):
    monkeypatch.setattr(worker.os, "getppid", lambda: 999999)
    assert worker._parent_alive(os.getpid())
    assert not worker._parent_alive(999999999)


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object regression")
def test_supervisor_job_object_bounds_windows_worker():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(5)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = None
    try:
        handle = worker._limit_windows_worker(process)
        assert isinstance(handle, int) and handle > 0
        assert process.poll() is None
    finally:
        worker._close_windows_job(handle)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
