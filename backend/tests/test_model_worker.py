"""Issue #58: hermetic model-worker, managed API and failure-state tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from finintel import main as backend_main
from finintel.market_data.analytics import AnalyticsError
from finintel.models import job_api, job_worker
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.job_store import ModelJobLedger, ModelJobRequest, ModelJobStoreError
from finintel.storage.db import _write_connection

REAL_WINDOWS_JOB_LIMIT = job_worker._limit_windows_worker

START = date(2026, 9, 1)
END = date(2026, 10, 5)
TOKEN = "local-managed-test-process-token-issue-58"


@pytest.fixture(autouse=True)
def keep_in_process_tests_unrestricted(monkeypatch):
    """Direct run_child fixture calls must not patch global sockets or process limits."""
    monkeypatch.setattr(job_worker, "_deny_outbound_network", lambda: None)
    monkeypatch.setattr(job_worker, "_bound_child_memory", lambda: None)
    # Mocked supervisor children do not own an OS process handle or PID.
    monkeypatch.setattr(job_worker, "_limit_windows_worker", lambda _process: None)
    monkeypatch.setattr(job_worker, "_macos_rss_bytes", lambda _pid: 1024)


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    market_db = tmp_path / "market.sqlite3"
    jobs_db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(market_db))
    monkeypatch.setenv("FININTEL_MODEL_JOBS_DB_PATH", str(jobs_db))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    # API tests focus on authorization and persistence without asynchronous jobs.
    monkeypatch.setattr(backend_main, "start_model_jobs", lambda: None)
    monkeypatch.setattr(backend_main, "stop_model_jobs", lambda: None)
    return market_db, jobs_db


def seed_market(db):
    conn = _write_connection(db)
    with conn:
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES (?,'fred',?,?,'2026-10-25T00:00:00+00:00',?)",
            [
                (
                    "SP500",
                    (START + timedelta(days=i)).isoformat(),
                    str(100 + i),
                    "https://fred.stlouisfed.org/series/SP500",
                )
                for i in range(35)
            ],
        )
    conn.close()


def request(snapshot):
    return ModelJobRequest(
        code="SP500",
        start=START,
        end=END,
        quality_policy="allow_unverified",
        expected_snapshot_sha256=snapshot,
    )


def job_api_body(**updates):
    return {
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "code": "SP500",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "quality_policy": "allow_unverified",
        "parameters": {"fast_window": 5, "slow_window": 20},
        **updates,
    }


def test_managed_job_api_rejects_missing_token_and_dynamic_modules(local_storage, monkeypatch):
    market, jobs = local_storage
    monkeypatch.setattr(job_api, "source_snapshot_for_job", lambda **_kwargs: "a" * 64)
    with TestClient(backend_main.app) as client:
        paths = [
            ("get", "/api/v1/models/jobs"),
            ("post", "/api/v1/models/jobs"),
            ("get", "/api/v1/models/jobs/00000000-0000-0000-0000-000000000000"),
            ("post", "/api/v1/models/jobs/00000000-0000-0000-0000-000000000000/cancel"),
            ("post", "/api/v1/models/jobs/00000000-0000-0000-0000-000000000000/retry"),
        ]
        for method, path in paths:
            response = getattr(client, method)(path)
            assert response.status_code == 403
        headers = {"x-finintel-process-token": TOKEN}
        assert (
            client.post(
                "/api/v1/models/jobs",
                json=job_api_body(module_path="/private/secret.py"),
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/models/jobs",
                json=job_api_body(model_id="dynamic.custom"),
                headers=headers,
            ).status_code
            == 422
        )
        assert not jobs.exists() and not market.exists()


def test_managed_enqueue_pins_local_snapshot_and_paginated_history(local_storage, monkeypatch):
    _, jobs = local_storage
    monkeypatch.setattr(job_api, "source_snapshot_for_job", lambda **_kwargs: "a" * 64)
    headers = {"x-finintel-process-token": TOKEN}
    with TestClient(backend_main.app) as client:
        stale = client.post(
            "/api/v1/models/jobs",
            json=job_api_body(expected_snapshot_sha256="b" * 64),
            headers=headers,
        )
        assert stale.status_code == 409
        assert not jobs.exists()
        queued = client.post("/api/v1/models/jobs", json=job_api_body(), headers=headers)
        assert queued.status_code == 202, queued.text
        identifier = queued.json()["id"]
        page = client.get("/api/v1/models/jobs", headers=headers)
        assert page.status_code == 200
        assert page.json()["total"] == 1
        assert len(page.json()["items"]) == 1
        assert (
            client.get(
                f"/api/v1/models/jobs/{identifier}?include_result=true",
                headers=headers,
            ).json()["result"]
            is None
        )
        assert client.get("/api/v1/models/jobs?limit=0", headers=headers).status_code == 422
        assert (
            client.post(
                f"/api/v1/models/jobs/{identifier}/retry",
                headers=headers,
            ).status_code
            == 409
        )
        cancelled = client.post(f"/api/v1/models/jobs/{identifier}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        again = client.post(f"/api/v1/models/jobs/{identifier}/retry", headers=headers)
        assert again.status_code == 200
        assert again.json()["state"] == "queued"
        claimed = ModelJobLedger(jobs).claim_next()
        assert claimed is not None
        assert claimed[1].expected_snapshot_sha256 == "a" * 64
        assert client.get("/api/v1/models/jobs/not-a-uuid", headers=headers).status_code == 422


def test_child_completes_only_pinned_observations_and_returns_verified_result(
    local_storage,
):
    market_db, jobs_db = local_storage
    seed_market(market_db)
    source = job_worker.source_snapshot_for_job(
        code="SP500",
        start=START,
        end=END,
        quality_policy="allow_unverified",
    )
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request(source))["id"]
    claim = ledger.claim_next()
    assert claim is not None and claim[0] == identifier
    # Exercise the real child interpreter and Linux RLIMIT, not a patched in-process mock.
    completed = subprocess.run(
        [sys.executable, "-m", "finintel.models.job_worker", "--child", identifier, str(claim[2])],
        env={
            **os.environ,
            "FININTEL_DB_PATH": str(market_db),
            "FININTEL_MODEL_JOBS_DB_PATH": str(jobs_db),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=40,
        check=False,
    )
    assert completed.returncode == 0, "Isolated model worker failed"
    finished = ledger.get(identifier, include_result=True)
    assert finished["state"] == "completed"
    assert finished["progress"] == 100
    assert finished["source_snapshot_sha256"] == source
    assert finished["result"]["output_sha256"] == finished["output_sha256"]
    assert finished["result"]["backtest_validated"] is False
    assert finished["result"]["persistent_job"] is False
    assert not ledger.set_progress(identifier, claim[2], 50)
    with sqlite3.connect(market_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_daily_closes").fetchone()[0] == 35


def test_changed_source_before_running_fails_closed(local_storage):
    market_db, jobs_db = local_storage
    seed_market(market_db)
    source = job_worker.source_snapshot_for_job(
        code="SP500",
        start=START,
        end=END,
        quality_policy="allow_unverified",
    )
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request(source))["id"]
    attempt = ledger.claim_next()[2]
    with sqlite3.connect(market_db) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='301',"
            " retrieved_at='2026-11-01T00:00:00+00:00'"
            " WHERE index_code='SP500' AND session_date='2026-09-08'"
        )
    assert job_worker.run_child(identifier, attempt) == 2
    assert ledger.get(identifier)["state"] == "failed"
    assert ledger.get(identifier)["error_code"] == "source_changed"


def test_changed_source_during_run_never_commits_old_result(local_storage, monkeypatch):
    market_db, jobs_db = local_storage
    seed_market(market_db)
    snapshot = job_worker.source_snapshot_for_job(
        code="SP500",
        start=START,
        end=END,
        quality_policy="allow_unverified",
    )
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request(snapshot))["id"]
    attempt = ledger.claim_next()[2]
    original = job_worker.evaluate_builtin_indicator

    def revise_during_evaluation(**params):
        result = original(**params)
        with sqlite3.connect(market_db) as conn:
            conn.execute(
                "UPDATE index_daily_closes SET close='420' "
                "WHERE index_code='SP500' AND session_date='2026-09-10'"
            )
        return result

    monkeypatch.setattr(job_worker, "evaluate_builtin_indicator", revise_during_evaluation)
    assert job_worker.run_child(identifier, attempt) == 2
    assert ledger.get(identifier, include_result=True)["result"] is None
    assert ledger.get(identifier)["error_code"] == "source_changed"


def test_stale_attempt_is_fenced_after_cancel_retry(local_storage):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    first = ledger.claim_next()
    assert first is not None
    assert ledger.cancel(identifier) == "cancel_requested"
    assert ledger.finish_cancelled(identifier, first[2])
    assert ledger.retry(identifier)
    second = ledger.claim_next()
    assert second is not None and second[2] != first[2]
    assert ledger.read_claim(identifier, first[2]) is None
    assert not ledger.fail(identifier, first[2], "worker_failure")
    assert not ledger.set_progress(identifier, first[2], 50)
    assert not ledger.finish_cancelled(identifier, first[2])
    assert ledger.get(identifier)["state"] == "running"


def test_two_supervisors_cannot_own_same_ledger(local_storage):
    _, jobs_db = local_storage
    first = job_worker.ModelJobSupervisor(ModelJobLedger(jobs_db))
    second = job_worker.ModelJobSupervisor(ModelJobLedger(jobs_db))
    assert first._acquire_lock()
    try:
        assert not second._acquire_lock()
    finally:
        first._release_lock()
    assert second._acquire_lock()
    second._release_lock()


class SimulatedChild:
    def __init__(self):
        self.pid = os.getpid()
        self.terminated = False
        self.exit_status = None

    def poll(self):
        return self.exit_status

    def terminate(self):
        self.terminated = True
        self.exit_status = -15

    def kill(self):
        self.terminated = True
        self.exit_status = -9

    def wait(self, timeout=None):
        return self.exit_status


def test_supervisor_cancels_child_without_orphan_or_late_result(
    local_storage,
    monkeypatch,
):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    claim = ledger.claim_next()
    assert claim is not None
    child = SimulatedChild()

    def spawned(*_args, **_kwargs):
        ledger.cancel(identifier)
        return child

    monkeypatch.setattr(job_worker.subprocess, "Popen", spawned)
    owner = job_worker.ModelJobSupervisor(ledger)
    owner._run_claim(*claim)
    assert child.terminated
    assert ledger.get(identifier)["state"] == "cancelled"
    assert owner._child is None


def test_supervisor_timeout_is_bounded_and_marks_safe_failure(local_storage, monkeypatch):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    claim = ledger.claim_next()
    child = SimulatedChild()
    monkeypatch.setattr(job_worker.subprocess, "Popen", lambda *_args, **_kw: child)
    ticks = iter([0.0, 1000.0])
    monkeypatch.setattr(job_worker.time, "monotonic", lambda: next(ticks, 1000.0))
    owner = job_worker.ModelJobSupervisor(ledger)
    owner._run_claim(*claim)
    assert child.terminated
    assert ledger.get(identifier)["state"] == "failed"
    assert ledger.get(identifier)["error_code"] == "resource_limit"


def test_worker_environment_never_passes_provider_keys(local_storage, monkeypatch):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    claim = ledger.claim_next()
    monkeypatch.setenv("FININTEL_ALPACA_API_KEY", "private-token")
    monkeypatch.setenv("OPENAI_API_KEY", "private-token")
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", TOKEN)
    captured = {}
    child = SimulatedChild()

    def spawned(*_args, **kwargs):
        captured.update(kwargs["env"])
        child.exit_status = 0
        return child

    monkeypatch.setattr(job_worker.subprocess, "Popen", spawned)
    owner = job_worker.ModelJobSupervisor(ledger)
    owner._run_claim(*claim)
    assert "FININTEL_ALPACA_API_KEY" not in captured
    assert "OPENAI_API_KEY" not in captured
    assert "FININTEL_PROCESS_TOKEN" not in captured
    assert captured["FININTEL_MODEL_JOBS_DB_PATH"] == str(jobs_db.resolve())
    assert ledger.get(identifier)["error_code"] == "worker_failure"


def test_wrong_worker_attempt_does_not_read_claim(local_storage):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    attempt = ledger.claim_next()[2]
    assert ledger.read_claim(identifier, attempt) is not None
    assert ledger.read_claim(identifier, attempt + 1) is None
    assert job_worker.run_child("not-a-uuid", attempt) == 2


def test_idle_supervisor_does_not_open_or_create_lock(local_storage):
    _, jobs_db = local_storage
    owner = job_worker.ModelJobSupervisor(ModelJobLedger(jobs_db))
    assert owner.start()
    try:
        time.sleep(0.35)
        assert not jobs_db.exists()
        assert not jobs_db.with_name(jobs_db.name + ".worker.lock").exists()
    finally:
        owner.stop()


def test_supervisor_lazily_takes_ownership_and_recovers_running_job(local_storage):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    assert ledger.claim_next() is not None
    owner = job_worker.ModelJobSupervisor(ledger)
    assert owner.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if ledger.get(identifier)["state"] == "failed":
                break
            time.sleep(0.05)
        assert ledger.get(identifier)["state"] == "failed"
        assert ledger.get(identifier)["error_code"] == "interrupted"
        assert jobs_db.with_name(jobs_db.name + ".worker.lock").exists()
    finally:
        owner.stop()


def test_managed_validation_does_not_echo_private_path_or_secret(local_storage, monkeypatch):
    headers = {"x-finintel-process-token": TOKEN}
    private_path = "/Users/private/keys/secret-model.py"
    with TestClient(backend_main.app) as client:
        response = client.post(
            "/api/v1/models/jobs",
            json=job_api_body(module_path=private_path),
            headers=headers,
        )
        assert response.status_code == 422
        assert private_path not in response.text
        assert "module_path" not in response.text
        response = client.post(
            "/api/v1/models/jobs",
            json=job_api_body(start="secret-backend-token"),
            headers=headers,
        )
        assert response.status_code == 422
        assert "secret-backend-token" not in response.text
        monkeypatch.setattr(job_api, "source_snapshot_for_job", lambda **_kw: "a" * 64)
        response = client.post(
            "/api/v1/models/jobs",
            json=job_api_body(start="2026-10-05", end="2026-09-01"),
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid local model-job request"}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_windows_worker_is_assigned_to_memory_limited_job():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(4)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    job_handle = None
    try:
        job_handle = REAL_WINDOWS_JOB_LIMIT(process)
        assert isinstance(job_handle, int) and job_handle > 0
        assert process.poll() is None
    finally:
        job_worker._stop_process(process)
        job_worker._close_windows_job(job_handle)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS resident-memory watchdog")
def test_macos_worker_rss_limit_terminates_child(local_storage, monkeypatch):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    claim = ledger.claim_next()
    assert claim is not None
    child = SimulatedChild()
    monkeypatch.setattr(job_worker.subprocess, "Popen", lambda *_args, **_kw: child)
    monkeypatch.setattr(
        job_worker,
        "_macos_rss_bytes",
        lambda _pid: job_worker.MAX_WORKER_MEMORY_BYTES + 1,
    )
    owner = job_worker.ModelJobSupervisor(ledger)
    owner._run_claim(*claim)
    assert child.terminated
    assert ledger.get(identifier)["state"] == "failed"
    assert ledger.get(identifier)["error_code"] == "resource_limit"


def test_worker_recovers_after_transient_claim_failure(local_storage, monkeypatch):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    original_claim = ledger.claim_next
    calls = 0

    def flaky_claim():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelJobStoreError("Temporary local model-job storage unavailable")
        return original_claim()

    monkeypatch.setattr(ledger, "claim_next", flaky_claim)
    owner = job_worker.ModelJobSupervisor(ledger)
    monkeypatch.setattr(
        owner,
        "_run_claim",
        lambda job_id, _request, attempt: ledger.fail(job_id, attempt, "worker_failure"),
    )
    assert owner.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if ledger.get(identifier)["state"] == "failed":
                break
            time.sleep(0.05)
        result = ledger.get(identifier)
        assert calls >= 2
        assert result["state"] == "failed"
        assert result["error_code"] == "worker_failure"
        assert result["attempts"] == 1
    finally:
        owner.stop()


def test_pending_cancellation_status_in_native_api_responses(local_storage):
    _, jobs_db = local_storage
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request("a" * 64))["id"]
    claim = ledger.claim_next()
    assert claim is not None
    assert ledger.cancel(identifier) == "cancel_requested"
    detail = ledger.get(identifier)
    assert detail["state"] == "running"
    assert detail["cancel_requested"] == 1
    assert detail["status"] == "cancel_requested"
    assert ledger.list()["items"][0]["status"] == "cancel_requested"
    headers = {"x-finintel-process-token": TOKEN}
    with TestClient(backend_main.app) as client:
        response = client.get(f"/api/v1/models/jobs/{identifier}", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "cancel_requested"
        response = client.get("/api/v1/models/jobs", headers=headers)
        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "cancel_requested"
    assert ledger.finish_cancelled(identifier, claim[2])
    assert ledger.get(identifier)["status"] == "cancelled"


def test_persisted_result_hashes_are_verified_on_read(local_storage):
    market_db, jobs_db = local_storage
    seed_market(market_db)
    snapshot = job_worker.source_snapshot_for_job(
        code="SP500", start=START, end=END, quality_policy="allow_unverified"
    )
    ledger = ModelJobLedger(jobs_db)
    identifier = ledger.enqueue(request(snapshot))["id"]
    claim = ledger.claim_next()
    assert claim is not None
    evaluation = evaluate_builtin_indicator(
        code="SP500",
        start=START,
        end=END,
        parameters=claim[1].parameters,
        quality_policy="allow_unverified",
        expected_snapshot_sha256=snapshot,
    )
    assert ledger.complete(identifier, claim[2], evaluation)
    original = ledger.get(identifier, include_result=True)["result"]
    assert original["output_sha256"] == ledger.get(identifier)["output_sha256"]

    with sqlite3.connect(jobs_db) as conn:
        for tampered in ("points", "lineage"):
            altered = json.loads(json.dumps(original))
            if tampered == "points":
                altered["points"][-1]["close"] = "9999"
            else:
                altered["lineage"]["quality_status"] = "forged"
            conn.execute(
                "UPDATE model_jobs SET result_json=? WHERE id=?",
                (json.dumps(altered), identifier),
            )
            conn.commit()
            with pytest.raises(ModelJobStoreError, match="integrity"):
                ledger.get(identifier, include_result=True)
            with TestClient(backend_main.app) as client:
                response = client.get(
                    f"/api/v1/models/jobs/{identifier}?include_result=true",
                    headers={"x-finintel-process-token": TOKEN},
                )
                assert response.status_code == 503
                assert "9999" not in response.text
                assert "forged" not in response.text
        conn.execute(
            "UPDATE model_jobs SET result_json=? WHERE id=?",
            (json.dumps(original), identifier),
        )
    assert ledger.get(identifier, include_result=True)["result"] == original


def test_safe_error_mapping_never_contains_exception_text():
    assert job_worker._safe_error_code(AnalyticsError("Local source snapshot changed")) == (
        "source_changed"
    )
    assert job_worker._safe_error_code(RuntimeError("private API password")) == "worker_failure"
