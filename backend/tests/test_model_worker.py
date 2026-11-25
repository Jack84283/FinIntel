"""Issue #58: hermetic model-worker, managed API and failure-state tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finintel import main as backend_main
from finintel.market_data.analytics import AnalyticsError
from finintel.models import analysis_job_api, job_api, job_worker, managed_analysis
from finintel.models.analysis_job_api import ANALYSIS_JOBS_PATH
from finintel.models.contracts import AnalysisRequest, AnalysisResult, DateRange, SourcePin
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.job_store import ModelJobLedger, ModelJobRequest, ModelJobStoreError
from finintel.models.managed_approvals import MANAGED_ANALYSIS_REVISIONS
from finintel.models.registry import PRICE_INDEX_SOURCES, get_model_definition
from finintel.storage.db import _write_connection

REAL_WINDOWS_JOB_LIMIT = job_worker._limit_windows_worker
REAL_MACOS_RSS = job_worker._macos_rss_bytes

START = date(2026, 9, 1)
END = date(2026, 10, 5)
TOKEN = "local-managed-test-process-token-issue-58"
MANAGED_HEADERS = {"x-finintel-process-token": TOKEN}


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


def _database_fingerprint(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        dump = "\n".join(conn.iterdump()).encode()
    return version, hashlib.sha256(dump).hexdigest()


def _execute_isolated(identifier, attempt):
    env = {key: value for key, value in os.environ.items() if key in job_worker.CHILD_ENV_KEYS}
    env.update(
        FININTEL_DB_PATH=os.environ["FININTEL_DB_PATH"],
        FININTEL_MODEL_JOBS_DB_PATH=os.environ["FININTEL_MODEL_JOBS_DB_PATH"],
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    worker = subprocess.run(
        [sys.executable, "-m", "finintel.models.job_worker", "--child", identifier, str(attempt)],
        stdin=subprocess.DEVNULL,
        env=env,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert worker.returncode == 0, "Isolated model worker failed"


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX packaged parent PID regression")
def test_worker_accepts_live_supervisor_through_bootloader(monkeypatch):
    # The immediate PPID belongs to PyInstaller, not the original sidecar.
    monkeypatch.setattr(job_worker.os, "getppid", lambda: 999999)
    assert job_worker._parent_alive(os.getpid())
    assert not job_worker._parent_alive(999999999)


def test_macos_rss_includes_frozen_child_process_tree(monkeypatch):
    monkeypatch.setattr(job_worker.sys, "platform", "darwin")
    sample = "100 1 10\n101 100 200\n102 101 300\n103 1 90000\n"
    result = subprocess.CompletedProcess([], 0, stdout=sample, stderr="")
    monkeypatch.setattr(job_worker.subprocess, "run", lambda *_a, **_kw: result)
    assert REAL_MACOS_RSS(100) == (10 + 200 + 300) * 1024
    assert REAL_MACOS_RSS(999999) is None


def test_safe_error_mapping_never_contains_exception_text():
    assert job_worker._safe_error_code(AnalyticsError("Local source snapshot changed")) == (
        "source_changed"
    )
    assert job_worker._safe_error_code(RuntimeError("private API password")) == "worker_failure"


def analysis_request(*, model_id="builtin.risk_profile", codes=("SP500",), parameters=None):
    return AnalysisRequest(
        model_id=model_id,
        model_version="1.0.0",
        window=DateRange(start=START, end=END),
        sources=tuple(
            SourcePin(
                identity=next(
                    identity for identity in PRICE_INDEX_SOURCES if identity.code == code
                ),
                snapshot_sha256=job_worker.source_snapshot_for_job(
                    code=code, start=START, end=END, quality_policy="allow_unverified"
                ),
            )
            for code in codes
        ),
        parameters=parameters or {},
        quality_policy="allow_unverified",
    )


def analysis_api_body(spec: AnalysisRequest, **updates) -> dict:
    return {
        **spec.model_dump(mode="json"),
        "implementation_revision": MANAGED_ANALYSIS_REVISIONS[(spec.model_id, spec.model_version)],
        **updates,
    }


@pytest.mark.parametrize(
    ("model_id", "parameters", "codes"),
    [
        ("builtin.ema_trend", {}, ("SP500",)),
        ("builtin.momentum", {"lookback_sessions": 21}, ("SP500",)),
        ("builtin.relative_strength", {}, ("NDX", "SP500")),
        ("builtin.rsi_state", {}, ("SP500",)),
        ("builtin.risk_profile", {}, ("NDX", "SP500")),
        ("builtin.market_regime", {}, ("SP500",)),
    ],
)
def test_real_child_executes_each_approved_analysis_model(
    local_storage, model_id, parameters, codes
):
    market, jobs = local_storage
    seed_market(market)
    if "NDX" in codes:
        with sqlite3.connect(market) as conn:
            conn.execute(
                "INSERT INTO index_daily_closes "
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "SELECT 'NDX',provider,session_date,close,retrieved_at,source_url "
                "FROM index_daily_closes WHERE index_code='SP500'"
            )
    before = _database_fingerprint(market)
    spec = analysis_request(model_id=model_id, codes=codes, parameters=parameters)
    ledger = ModelJobLedger(jobs)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    created = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec))
    assert created.status_code == 202
    identifier = created.json()["id"]
    claimed = ledger.claim_next()
    assert claimed is not None and claimed[0] == identifier and claimed[2] == 1
    assert list(claimed[1].sources) == list(spec.sources)
    assert all(claimed[1].parameters[key] == value for key, value in parameters.items())

    _execute_isolated(identifier, 1)
    detail = client.get(f"{ANALYSIS_JOBS_PATH}/{identifier}?include_result=true")
    assert detail.status_code == 200
    finished = detail.json()
    assert finished["state"] == "completed"
    result = AnalysisResult.model_validate_json(json.dumps(finished["result"]))
    assert result.evidence.observed_sessions == tuple(START + timedelta(days=i) for i in range(35))
    assert _database_fingerprint(market) == before
    assert finished["result"]["evidence"]["model"]["model_id"] == model_id
    assert [
        source["identity"]["code"] for source in finished["result"]["evidence"]["sources"]
    ] == list(codes)
    assert finished["result"]["output_sha256"] == finished["output_sha256"]
    assert finished["source_snapshot_sha256"] == (
        spec.sources[0].snapshot_sha256 if len(codes) == 1 else None
    )
    assert [source["snapshot_sha256"] for source in finished["result"]["evidence"]["sources"]] == [
        source.snapshot_sha256 for source in spec.sources
    ]
    assert client.get(f"/api/v1/models/jobs/{identifier}").status_code == 404
    assert ledger.get(identifier, legacy_only=True) is None
    assert not ledger.retry(identifier, legacy_only=False)


def test_analysis_attempt_rechecks_source_and_retry_keeps_original_pin(local_storage, monkeypatch):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    identifier = job_worker.enqueue_analysis_job(spec)["id"]
    _, claimed, first_attempt = ledger.claim_next()
    evaluate = managed_analysis._MANAGED_EVALUATORS[(spec.model_id, spec.model_version)]

    def evaluate_then_revise(request, *, db=None):
        result = evaluate(request, db=db)
        with sqlite3.connect(market) as conn:
            conn.execute(
                "UPDATE index_daily_closes SET close='999' WHERE session_date=?",
                (START.isoformat(),),
            )
        return result

    monkeypatch.setattr(
        managed_analysis,
        "_MANAGED_EVALUATORS",
        {(spec.model_id, spec.model_version): evaluate_then_revise},
    )
    assert job_worker.run_child(identifier, first_attempt) == 2
    failed = ledger.get(identifier, include_result=True)
    assert failed["error_code"] == "source_changed"
    assert failed["result"] is None
    assert ledger.retry(identifier, legacy_only=False)
    _, retried, second_attempt = ledger.claim_next()
    assert retried == claimed
    assert second_attempt == first_attempt + 1
    assert job_worker.run_child(identifier, first_attempt) == 0
    assert ledger.get(identifier)["state"] == "running"
    assert job_worker.run_child(identifier, second_attempt) == 2
    assert ledger.get(identifier)["error_code"] == "source_changed"


def test_analysis_preflight_rejects_missing_history_and_unknown_approval(
    local_storage, monkeypatch
):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request(model_id="builtin.momentum")
    with pytest.raises(AnalyticsError, match="64 shared observed sessions"):
        job_worker.enqueue_analysis_job(spec)
    eligible = analysis_request()
    with pytest.raises(AnalyticsError, match="Unsupported managed analysis model or version"):
        job_worker.enqueue_analysis_job(eligible.model_copy(update={"model_version": "2.0.0"}))
    monkeypatch.setattr(
        managed_analysis,
        "MANAGED_ANALYSIS_REVISIONS",
        {(eligible.model_id, eligible.model_version): "unapproved-revision"},
    )
    with pytest.raises(
        AnalyticsError, match="Unsupported managed analysis implementation revision"
    ):
        job_worker.enqueue_analysis_job(eligible)
    assert not jobs.exists()


def test_corrupt_queued_analysis_request_does_not_starve_legacy_jobs(local_storage):
    market, jobs = local_storage
    seed_market(market)
    ledger = ModelJobLedger(jobs)
    identifier = job_worker.enqueue_analysis_job(analysis_request())["id"]
    following = ledger.enqueue(request("a" * 64))["id"]
    with sqlite3.connect(jobs) as conn:
        conn.execute("UPDATE model_jobs SET request_sha256=? WHERE id=?", ("b" * 64, identifier))
    assert ledger.claim_next() is None
    assert ledger.get(identifier)["error_code"] == "invalid_input"
    assert ledger.claim_next()[0] == following


def test_analysis_rejects_quality_change_with_unchanged_snapshot(local_storage, monkeypatch):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    identifier = job_worker.enqueue_analysis_job(spec)["id"]
    ledger = ModelJobLedger(jobs)
    _, _, attempt = ledger.claim_next()
    evaluate = managed_analysis._MANAGED_EVALUATORS[(spec.model_id, spec.model_version)]

    def evaluate_then_change_quality(request, *, db=None):
        result = evaluate(request, db=db)
        now = datetime.now(UTC)
        with sqlite3.connect(market) as conn:
            conn.execute(
                "INSERT INTO data_quality_scan_progress "
                "(scan_key,source_identity,source_data_version_start,source_data_version_end,"
                "cursor_start,cursor_next,max_findings,findings_examined,findings_stored,"
                "total_known_findings,persisted_findings_total,status,stale,updated_at,"
                "validation_reference_date) "
                "SELECT 'all','fixture',source_version,source_version,0,0,1,0,0,0,0,"
                "'partial',0,?,? FROM data_quality_source_state WHERE id=1",
                (now.isoformat(), now.date().isoformat()),
            )
        return result

    monkeypatch.setattr(
        managed_analysis,
        "_MANAGED_EVALUATORS",
        {(spec.model_id, spec.model_version): evaluate_then_change_quality},
    )
    assert job_worker.run_child(identifier, attempt) == 2
    failed = ledger.get(identifier, include_result=True)
    assert failed["error_code"] == "quality_blocked"
    assert failed["result"] is None
    assert (
        job_worker.source_snapshot_for_job(
            code="SP500", start=START, end=END, quality_policy="allow_unverified"
        )
        == spec.sources[0].snapshot_sha256
    )


def test_analysis_cancelled_and_interrupted_attempts_cannot_complete(local_storage, monkeypatch):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    cancelled = job_worker.enqueue_analysis_job(spec)["id"]
    _, _, first_attempt = ledger.claim_next()
    evaluate = job_worker.evaluate_analysis_job

    def evaluate_then_cancel(request):
        result = evaluate(request)
        assert ledger.cancel(cancelled) == "cancel_requested"
        return result

    monkeypatch.setattr(job_worker, "evaluate_analysis_job", evaluate_then_cancel)
    assert job_worker.run_child(cancelled, first_attempt) == 0
    finished = ledger.get(cancelled, include_result=True)
    assert finished["state"] == "cancelled" and finished["result"] is None

    interrupted = job_worker.enqueue_analysis_job(spec)["id"]
    _, _, disposed = ledger.claim_next()
    pending = job_worker.enqueue_analysis_job(spec)["id"]
    assert ledger.reconcile_interrupted() == 1
    assert ledger.get(interrupted)["error_code"] == "interrupted"
    assert ledger.get(pending)["state"] == "queued"
    assert job_worker.run_child(interrupted, disposed) == 0
    assert ledger.get(interrupted, include_result=True)["result"] is None


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_source_writer_is_excluded_through_ledger_commit(local_storage, monkeypatch, journal_mode):
    market, jobs = local_storage
    seed_market(market)
    with sqlite3.connect(market) as conn:
        assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    identifier = job_worker.enqueue_analysis_job(spec)["id"]
    _, _, attempt = ledger.claim_next()
    complete = ModelJobLedger.complete_analysis

    def complete_with_competing_source_writer(self, identifier, attempt, result):
        with (
            sqlite3.connect(market, timeout=0) as writer,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            writer.execute(
                "UPDATE index_daily_closes SET close='999' WHERE session_date=?",
                (START.isoformat(),),
            )
        return complete(self, identifier, attempt, result)

    monkeypatch.setattr(ModelJobLedger, "complete_analysis", complete_with_competing_source_writer)
    assert job_worker.run_child(identifier, attempt) == 0
    finished = ledger.get(identifier, include_result=True)
    assert finished["state"] == "completed"
    with sqlite3.connect(market, timeout=0) as writer:
        writer.execute(
            "UPDATE index_daily_closes SET close='999' WHERE session_date=?", (START.isoformat(),)
        )
    assert ledger.get(identifier, include_result=True) == finished


def test_source_commit_lock_contention_fails_closed_and_retry_releases_lock(local_storage):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    identifier = job_worker.enqueue_analysis_job(spec)["id"]
    _, _, attempt = ledger.claim_next()
    with sqlite3.connect(market) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE index_daily_closes SET close='999' WHERE session_date=?", (START.isoformat(),)
        )
        assert job_worker.run_child(identifier, attempt) == 2
        failed = ledger.get(identifier, include_result=True)
        assert failed["error_code"] == "storage_unavailable"
        assert failed["result"] is None
        writer.rollback()

    assert ledger.retry(identifier, legacy_only=False)
    _, _, retry = ledger.claim_next()
    assert retry == attempt + 1
    assert job_worker.run_child(identifier, retry) == 0
    assert ledger.get(identifier, include_result=True)["state"] == "completed"


def test_analysis_api_authenticates_every_route_before_validation(local_storage):
    client = TestClient(backend_main.app)
    identifier = "123e4567-e89b-12d3-a456-426614174000"
    for headers in ({}, {"x-finintel-process-token": "wrong"}):
        for method, path in (
            ("POST", ""),
            ("GET", "?limit=-5"),
            ("GET", f"/{identifier}"),
            ("POST", f"/{identifier}/cancel"),
            ("POST", f"/{identifier}/retry"),
        ):
            reply = client.request(
                method, ANALYSIS_JOBS_PATH + path, content=b"{broken", headers=headers
            )
            assert reply.status_code == 403
            assert reply.json() == {"detail": "managed_required"}


def test_analysis_api_rejects_model_and_source_contract_changes(local_storage):
    market, _ = local_storage
    seed_market(market)
    spec = analysis_request()
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    source = spec.sources[0].model_dump(mode="json")
    malformed = [
        {"model_id": "builtin.sma_crossover"},
        {"model_version": "2.0.0"},
        {"implementation_revision": "unapproved"},
        {"parameters": {"risk_window": 1}},
        {"parameters": {"tail_window": "60"}},
        {"parameters": {"private_path": "/tmp/private-file"}},
        {"sources": []},
        {"sources": [source, source]},
        {"sources": [{"identity": source["identity"]}]},
        {"sources": [{**source, "snapshot_sha256": "F" * 64}]},
        {"window": {"start": "2026-09-31", "end": END.isoformat()}},
    ]
    for updates in malformed:
        response = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec, **updates))
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_input"}
    response = client.post(
        ANALYSIS_JOBS_PATH, json=analysis_api_body(spec, quality_policy="require_complete")
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "quality_blocked"}
    forged_pin = {**source, "snapshot_sha256": "0" * 64}
    response = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec, sources=[forged_pin]))
    assert response.status_code == 409
    assert response.json() == {"detail": "source_changed"}
    with sqlite3.connect(market) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='1234' WHERE session_date=?",
            (START.isoformat(),),
        )
    response = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec))
    assert response.status_code == 409
    assert response.json() == {"detail": "source_changed"}
    assert client.get(ANALYSIS_JOBS_PATH).json() == {"total": 0, "items": []}


def test_analysis_history_paginates_filtered_jobs_and_shares_queue_quota(local_storage):
    market, _ = local_storage
    seed_market(market)
    spec = analysis_request()
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    legacy = client.post("/api/v1/models/jobs", json=job_api_body())
    assert legacy.status_code == 202
    legacy_id = legacy.json()["id"]
    for suffix in ("", "/cancel", "/retry"):
        reply = client.request(
            "POST" if suffix else "GET", f"{ANALYSIS_JOBS_PATH}/{legacy_id}{suffix}"
        )
        assert reply.status_code == 404
        assert reply.json() == {"detail": "job_not_found"}
    assert client.get(f"/api/v1/models/jobs/{legacy_id}").json()["state"] == "queued"

    identifiers = []
    for _ in range(31):
        response = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec))
        assert response.status_code == 202
        identifiers.append(response.json()["id"])
    full = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec))
    assert full.status_code == 409
    assert full.json() == {"detail": "queue_full"}
    for identifier in identifiers[:12]:
        assert client.post(f"{ANALYSIS_JOBS_PATH}/{identifier}/cancel").json() == {
            "state": "cancelled"
        }
    cancelled = client.get(ANALYSIS_JOBS_PATH, params={"status": "cancelled"}).json()
    assert cancelled["total"] == 12
    assert len(cancelled["items"]) == 10
    next_page = client.get(ANALYSIS_JOBS_PATH, params={"status": "cancelled", "offset": 10}).json()
    assert next_page["total"] == 12
    assert {item["id"] for item in cancelled["items"] + next_page["items"]} == set(identifiers[:12])
    assert client.get(ANALYSIS_JOBS_PATH).json()["total"] == 31
    assert client.get("/api/v1/models/jobs").json()["total"] == 1
    for suffix in ("?limit=101", "?offset=10001", "?status=private", "/not-a-job"):
        invalid = client.get(ANALYSIS_JOBS_PATH + suffix)
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "invalid_input"}


def test_analysis_retry_fences_attempts_and_historical_detail_checks_integrity(local_storage):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    queued = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec)).json()
    identifier = queued["id"]
    detail = f"{ANALYSIS_JOBS_PATH}/{identifier}"
    assert client.post(detail + "/retry").json() == {"detail": "job_conflict"}
    assert client.get(detail, params={"include_result": True}).json()["result"] is None
    _, original, first = ledger.claim_next()
    assert ledger.reconcile_interrupted() == 1
    assert client.get(detail).json()["error_code"] == "interrupted"
    retry = client.post(detail + "/retry")
    assert retry.status_code == 200
    assert retry.json()["request_sha256"] == queued["request_sha256"]
    _, current, second = ledger.claim_next()
    assert current == original
    assert second == first + 1
    assert not ledger.complete_analysis(
        identifier, first, managed_analysis.evaluate_analysis_job(original)
    )
    assert client.get(detail).json()["state"] == "running"
    assert job_worker.run_child(identifier, second) == 0
    original_detail = client.get(detail, params={"include_result": True}).json()
    with sqlite3.connect(market) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close='1234' WHERE session_date=?",
            (START.isoformat(),),
        )
    assert client.get(detail, params={"include_result": True}).json() == original_detail
    with sqlite3.connect(jobs) as conn:
        conn.execute("UPDATE model_jobs SET result_json='{}' WHERE id=?", (identifier,))
    response = client.get(detail, params={"include_result": True})
    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}


def test_analysis_transport_schema_tracks_worker_approvals():
    path = Path(__file__).resolve().parents[2] / "contracts/analysis-jobs.json"
    contracts = json.loads(path.read_text(encoding="utf-8"))
    assert set(contracts) == {model_id for model_id, _ in MANAGED_ANALYSIS_REVISIONS}
    for (model_id, version), revision in MANAGED_ANALYSIS_REVISIONS.items():
        request = AnalysisRequest(
            model_id=model_id,
            model_version=version,
            sources=(SourcePin(identity=PRICE_INDEX_SOURCES[0], snapshot_sha256="a" * 64),),
            window=DateRange(start=date(2020, 1, 1), end=date(2020, 2, 1)),
        )
        definition = get_model_definition(request)
        metadata = definition.metadata
        assert contracts[model_id] == {
            "version": version,
            "revision": revision,
            "calculation": metadata.calculation_version,
            "min_sources": metadata.min_sources,
            "max_sources": metadata.max_sources,
            "allows_unverified": metadata.allows_unverified,
            "parameters": {
                name: {
                    key: value for key, value in rule.items() if key not in {"title", "description"}
                }
                for name, rule in definition.parameter_type.model_json_schema()[
                    "properties"
                ].items()
            },
            "output_fields": [field.model_dump(mode="json") for field in metadata.output_fields],
        }


def test_slow_analysis_retry_cannot_requeue_a_newer_failed_attempt(local_storage, monkeypatch):
    market, jobs = local_storage
    seed_market(market)
    spec = analysis_request()
    ledger = ModelJobLedger(jobs)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    identifier = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec)).json()["id"]
    detail = f"{ANALYSIS_JOBS_PATH}/{identifier}"
    _, _, first = ledger.claim_next()
    assert ledger.fail(identifier, first, "worker_failure")
    preflight = analysis_job_api.preflight_analysis_job

    def interleaved_preflight(request):
        preflight(request)
        assert ledger.retry(identifier, analysis_only=True, expected_attempt=first)
        claimed_id, _, second = ledger.claim_next()
        assert claimed_id == identifier and second == first + 1
        assert ledger.fail(identifier, second, "worker_failure")

    monkeypatch.setattr(analysis_job_api, "preflight_analysis_job", interleaved_preflight)
    reply = client.post(detail + "/retry")
    assert reply.status_code == 409
    assert reply.json() == {"detail": "job_conflict"}
    job = client.get(detail).json()
    assert job["state"] == "failed" and job["attempts"] == first + 1


def _verified_market_quality(market):
    now = datetime.now(UTC)
    with sqlite3.connect(market) as conn:
        conn.execute(
            "INSERT INTO data_quality_scan_progress "
            "(scan_key,source_identity,source_data_version_start,source_data_version_end,"
            "cursor_start,cursor_next,max_findings,findings_examined,findings_stored,"
            "total_known_findings,persisted_findings_total,status,stale,updated_at,"
            "validation_reference_date) "
            "SELECT 'all','fixture',source_version,source_version,0,0,1,0,0,0,0,"
            "'completed',0,?,? FROM data_quality_source_state WHERE id=1",
            (now.isoformat(), now.date().isoformat()),
        )


def _backtest_body(client, *, policy="require_complete", isolated=False, sessions=None):
    from finintel.models.backtest import BACKTEST_REVISION, BacktestRequest

    dates = sessions or tuple(START + timedelta(days=i) for i in range(35))
    queued = client.post(
        "/api/v1/models/jobs",
        json=job_api_body(
            quality_policy=policy, start=dates[0].isoformat(), end=dates[-1].isoformat()
        ),
    )
    assert queued.status_code == 202, queued.json()
    source_id = queued.json()["id"]
    assert ModelJobLedger().claim_next()[0] == source_id
    if isolated:
        _execute_isolated(source_id, 1)
    else:
        assert job_worker.run_child(source_id, 1) == 0
    source = ModelJobLedger().get(source_id, include_result=True, legacy_only=True)
    research = source["result"]
    simulation = BacktestRequest(
        index_code="SP500",
        expected_snapshot_sha256=source["source_snapshot_sha256"],
        training_end=dates[19] if sessions else dates[30],
        test_start=dates[20] if sessions else dates[31],
        test_end=dates[-1] if sessions else dates[33],
    )
    return source, {
        "contract_version": "backtest-job-request-v1",
        "source_job_id": source_id,
        "source_request_sha256": source["request_sha256"],
        "source_output_sha256": source["output_sha256"],
        "source_lineage_sha256": research["reproducibility_sha256"],
        "source_implementation_revision": research["lineage"]["implementation_revision"],
        "engine_revision": BACKTEST_REVISION,
        "simulation": simulation.model_dump(mode="json"),
    }


def test_durable_backtest_uses_frozen_source_and_persists_exact_engine_report(local_storage):
    from finintel.models.backtest import BacktestRequest, run_sma_backtest
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    before = _database_fingerprint(market)
    source, body = _backtest_body(client, isolated=True)
    assert TestClient(backend_main.app).post(BACKTEST_JOBS_PATH, json=body).status_code == 403
    assert client.post(BACKTEST_JOBS_PATH, json={**body, "module": "/private/file"}).json() == {
        "detail": "invalid_input"
    }
    assert client.post(
        BACKTEST_JOBS_PATH, json={**body, "source_output_sha256": "a" * 64}
    ).json() == {"detail": "source_changed"}
    created = client.post(BACKTEST_JOBS_PATH, json=body)
    assert created.status_code == 202, created.json()
    identifier = created.json()["id"]
    route = f"{BACKTEST_JOBS_PATH}/{identifier}"
    assert client.get(f"/api/v1/models/jobs/{identifier}").status_code == 404
    assert client.post(f"/api/v1/models/jobs/{identifier}/cancel").status_code == 404
    assert client.get(f"{ANALYSIS_JOBS_PATH}/{identifier}").status_code == 404
    assert client.get("/api/v1/models/jobs").json()["total"] == 1
    assert client.get(BACKTEST_JOBS_PATH, params={"status": "queued"}).json()["total"] == 1
    ledger = ModelJobLedger()
    _, _, attempt = ledger.claim_next()
    assert ledger.claim_next() is None
    _execute_isolated(identifier, attempt)
    detail = client.get(route, params={"include_result": "true"}).json()
    assert detail["state"] == "completed"
    result = detail["result"]
    expected = run_sma_backtest(
        research=source["result"],
        request=BacktestRequest.model_validate_json(json.dumps(body["simulation"])),
    )
    assert result["report"] == expected
    assert result["request_sha256"] == detail["request_sha256"]
    assert expected["transactions"][0]["signal_as_of_session"] == body["simulation"]["training_end"]

    assert (
        expected["transactions"][0]["hypothetical_fill_session"] == body["simulation"]["test_start"]
    )
    assert _database_fingerprint(market) == before

    with sqlite3.connect(market) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close=1234 WHERE session_date=?", (START.isoformat(),)
        )
    assert client.post(BACKTEST_JOBS_PATH, json=body).json() == {"detail": "source_changed"}
    assert (
        ModelJobLedger().get(identifier, include_result=True, backtest_only=True)["result"]
        == result
    )
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "UPDATE model_jobs SET result_json=replace(result_json,'equity_after_cost','tampered') WHERE id=?",
            (identifier,),
        )
    assert client.get(route, params={"include_result": "true"}).json() == {
        "detail": "storage_unavailable"
    }


@pytest.mark.parametrize("source_verified", [True, False])
def test_backtest_rejects_original_override_even_if_source_is_verified(
    local_storage, source_verified
):
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    if source_verified:
        _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    source, body = _backtest_body(client, policy="allow_unverified")
    assert source["result"]["lineage"]["quality_verified"] is source_verified
    if not source_verified:
        _verified_market_quality(market)
    assert client.post(BACKTEST_JOBS_PATH, json=body).json() == {"detail": "quality_blocked"}


def test_backtest_rechecks_sources_and_fences_cancelled_and_interrupted_attempts(
    local_storage, monkeypatch
):
    from finintel.models import backtest_worker
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    _, body = _backtest_body(client)
    identifier = client.post(BACKTEST_JOBS_PATH, json=body).json()["id"]
    route = f"{BACKTEST_JOBS_PATH}/{identifier}"
    ledger = ModelJobLedger()
    _, _, first = ledger.claim_next()
    assert client.post(route + "/cancel").json() == {"state": "cancel_requested"}
    assert ledger.reconcile_interrupted() == 1
    assert ledger.get(identifier)["state"] == "cancelled"
    assert client.post(route + "/retry").json()["request"] == body
    _, _, second = ledger.claim_next()
    assert second == 2
    assert job_worker.run_child(identifier, first) == 0
    assert ledger.get(identifier)["state"] == "running"
    simulate = backtest_worker.run_sma_backtest

    def change_after_simulation(**kwargs):
        result = simulate(**kwargs)
        with sqlite3.connect(market) as conn:
            conn.execute(
                "UPDATE index_daily_closes SET close=1234 WHERE session_date=?",
                (START.isoformat(),),
            )
        return result

    monkeypatch.setattr(backtest_worker, "run_sma_backtest", change_after_simulation)
    assert job_worker.run_child(identifier, second) == 2
    assert ledger.get(identifier)["error_code"] == "source_changed"
    assert client.post(route + "/retry").json() == {"detail": "source_changed"}


def test_backtest_final_verification_keeps_source_writer_reserved(local_storage, monkeypatch):
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    _, body = _backtest_body(client)
    identifier = client.post(BACKTEST_JOBS_PATH, json=body).json()["id"]
    ledger = ModelJobLedger()
    _, _, attempt = ledger.claim_next()
    persist = ModelJobLedger._persist_result
    verified = []

    def try_concurrent_write(*args, **kwargs):
        with (
            sqlite3.connect(market, timeout=0) as writer,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            writer.execute("BEGIN IMMEDIATE")
        verified.append(True)
        return persist(*args, **kwargs)

    monkeypatch.setattr(ModelJobLedger, "_persist_result", staticmethod(try_concurrent_write))
    assert job_worker.run_child(identifier, attempt) == 0
    assert verified == [True]
    assert ledger.get(identifier)["state"] == "completed"


def test_backtest_rejects_stale_quality_bad_windows_and_incompatible_semantics(local_storage):
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    _, body = _backtest_body(client)
    for change in (
        {"training_end": (START + timedelta(days=19)).isoformat()},
        {"index_code": "VIX"},
        {"source_semantics": "adjusted_price"},
    ):
        bad = {**body, "simulation": {**body["simulation"], **change}}
        response = client.post(BACKTEST_JOBS_PATH, json=bad)
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_input"}

    with sqlite3.connect(market) as conn:
        conn.execute("UPDATE data_quality_scan_progress SET stale=1 WHERE scan_key='all'")
    assert client.post(BACKTEST_JOBS_PATH, json=body).json() == {"detail": "quality_blocked"}


def test_backtest_authenticates_each_operation_before_processing(local_storage):
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    identifier = "123e4567-e89b-12d3-a456-426614174000"
    client = TestClient(backend_main.app)
    for headers in ({}, {"x-finintel-process-token": "wrong"}):
        for method, path in (
            ("POST", ""),
            ("GET", ""),
            ("GET", f"/{identifier}"),
            ("POST", f"/{identifier}/retry"),
            ("POST", f"/{identifier}/cancel"),
        ):
            response = client.request(method, BACKTEST_JOBS_PATH + path, headers=headers)
            assert response.status_code == 403
            assert response.json() == {"detail": "managed_required"}


def test_backtest_fails_closed_on_source_writer_lock_contention(local_storage):
    from contextlib import closing

    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, _ = local_storage
    seed_market(market)
    _verified_market_quality(market)
    client = TestClient(backend_main.app, headers=MANAGED_HEADERS)
    _, body = _backtest_body(client)
    identifier = client.post(BACKTEST_JOBS_PATH, json=body).json()["id"]
    ledger = ModelJobLedger()
    _, _, attempt = ledger.claim_next()
    with closing(sqlite3.connect(market)) as writer, writer:
        writer.execute("BEGIN IMMEDIATE")
        assert job_worker.run_child(identifier, attempt) == 2
    job = ledger.get(identifier, include_result=True)
    assert job["error_code"] == "storage_unavailable"
    assert job["result"] is None
    assert client.post(f"{BACKTEST_JOBS_PATH}/{identifier}/retry").json()["request"] == body


def test_al09_market_restore_preserves_separate_completed_ledger(local_storage, monkeypatch):
    from finintel.storage import backup

    market, jobs = local_storage
    seed_market(market)
    _verified_market_quality(market)
    with TestClient(backend_main.app, headers=MANAGED_HEADERS) as client:
        source, _ = _backtest_body(client, isolated=True)
    original_market = _database_fingerprint(market)
    original_ledger = _database_fingerprint(jobs)
    snapshot = backup.create_backup(db=market)

    # This shares the synthetic backup directory but has no destination database.
    empty = market.with_name("empty.sqlite3")
    backup.restore_backup(snapshot["id"], "replace", db=empty)
    assert _database_fingerprint(empty) == original_market

    with sqlite3.connect(market) as conn:
        conn.execute(
            "UPDATE index_daily_closes SET close = '999' WHERE session_date = ?",
            (START.isoformat(),),
        )
    revised_market = _database_fingerprint(market)
    assert revised_market != original_market

    real_copy = backup._copy_sqlite

    def interrupt_first_destination_write(incoming, destination):
        if destination == market and incoming.name == "incoming.sqlite3":
            real_copy(incoming, destination)
            raise OSError("synthetic interruption after destination write")
        real_copy(incoming, destination)

    with monkeypatch.context() as patch:
        patch.setattr(backup, "_copy_sqlite", interrupt_first_destination_write)
        with pytest.raises(backup.StorageError, match="prior database was recovered"):
            backup.restore_backup(snapshot["id"], "replace", db=market)
    assert _database_fingerprint(market) == revised_market
    backup.restore_backup(snapshot["id"], "replace", db=market)
    assert _database_fingerprint(market) == original_market
    assert _database_fingerprint(jobs) == original_ledger
    reopened = ModelJobLedger(jobs).get(source["id"], include_result=True, legacy_only=True)
    assert reopened["result"] == source["result"]


def _seed_maximum_window(market, codes):
    sessions = tuple(date(2017, 1, 2) + timedelta(days=i + i // 5 * 2) for i in range(2000))
    with _write_connection(market) as conn:
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES (?,'fred',?,?,'2026-01-02T00:00:00+00:00',?)",
            [
                (
                    code,
                    session.isoformat(),
                    str(1000 + index + i + (i % 13)),
                    f"https://fred.stlouisfed.org/series/{code}",
                )
                for index, code in enumerate(codes)
                for i, session in enumerate(sessions)
            ],
        )
    conn.close()
    _verified_market_quality(market)
    return sessions


@pytest.mark.parametrize(
    ("model", "codes"),
    [
        ("builtin.relative_strength", ("NDX", "DJIA", "SP500", "NASDAQCOM")),
        ("builtin.market_regime", ("SP500",)),
    ],
)
def test_al09_maximum_observed_window(local_storage, model, codes):
    market, jobs = local_storage
    sessions = _seed_maximum_window(market, codes)
    before = _database_fingerprint(market)
    spec = AnalysisRequest(
        model_id=model,
        model_version="1.0.0",
        window=DateRange(start=sessions[0], end=sessions[-1]),
        sources=tuple(
            SourcePin(
                identity=next(
                    identity for identity in PRICE_INDEX_SOURCES if identity.code == code
                ),
                snapshot_sha256=job_worker.source_snapshot_for_job(
                    code=code,
                    start=sessions[0],
                    end=sessions[-1],
                    quality_policy="require_complete",
                ),
            )
            for code in codes
        ),
        parameters={},
        quality_policy="require_complete",
    )
    with TestClient(backend_main.app, headers=MANAGED_HEADERS) as client:
        queued = client.post(ANALYSIS_JOBS_PATH, json=analysis_api_body(spec))
        assert queued.status_code == 202, queued.json()
        identifier = queued.json()["id"]
        assert ModelJobLedger(jobs).claim_next()[0] == identifier
        _execute_isolated(identifier, 1)
        detail = client.get(f"{ANALYSIS_JOBS_PATH}/{identifier}?include_result=true")
        assert detail.status_code == 200
    result = AnalysisResult.model_validate_json(json.dumps(detail.json()["result"]))
    assert result.evidence.observed_sessions == sessions
    assert [source.identity.code for source in result.evidence.sources] == list(codes)
    assert not result.evidence.warnings
    assert all(source.quality.status == "verified" for source in result.evidence.sources)
    if model == "builtin.market_regime":
        assert result.points[0]["volatility_state"] == "unknown"
        assert result.points[-1]["volatility_state"] != "unknown"
        assert result.points[-1]["volatility_calibration_count"] == 756
    assert _database_fingerprint(market) == before


def test_al09_maximum_backtest_source_window(local_storage):
    from finintel.models.backtest_job_api import BACKTEST_JOBS_PATH

    market, jobs = local_storage
    sessions = _seed_maximum_window(market, ("SP500",))
    before = _database_fingerprint(market)
    with TestClient(backend_main.app, headers=MANAGED_HEADERS) as client:
        source, body = _backtest_body(client, isolated=True, sessions=sessions)
        assert source["result"]["observations"] == 2000
        queued = client.post(BACKTEST_JOBS_PATH, json=body)
        assert queued.status_code == 202, queued.json()
        identifier = queued.json()["id"]
        assert ModelJobLedger(jobs).claim_next()[0] == identifier
        _execute_isolated(identifier, 1)
        detail = client.get(f"{BACKTEST_JOBS_PATH}/{identifier}?include_result=true")
        assert detail.status_code == 200
    payload = detail.json()
    assert payload["state"] == "completed"
    assert payload["result"]["request_sha256"] == payload["request_sha256"]
    report = payload["result"]["report"]
    assert [point["session_date"] for point in report["rows"]] == [
        session.isoformat() for session in sessions[20:]
    ]
    assert _database_fingerprint(market) == before
