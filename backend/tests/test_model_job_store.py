"""Synthetic, standalone durable model-job ledger tests (no private data)."""

import hashlib
import json
import sqlite3
import stat
import uuid
from contextlib import closing
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finintel.main import app
from finintel.market_data.analytics_store import _read_series
from finintel.models import job_store
from finintel.models.contracts import AnalysisRequest, AnalysisResult, DateRange, SourcePin
from finintel.models.job_store import (
    ANALYSIS_REQUEST_CONTRACT,
    JOB_SCHEMA_VERSION,
    LEGACY_REQUEST_CONTRACT,
    MAX_ACTIVE_JOBS,
    MAX_ATTEMPTS,
    ModelJobLedger,
    ModelJobRequest,
    ModelJobStoreError,
    analysis_job_request_payload,
)
from finintel.models.registry import PRICE_INDEX_SOURCES, RISK_MODEL_ID
from finintel.models.risk import evaluate_risk_model
from finintel.storage.db import _write_connection


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
            "instrument_kind": "index",
            "code": "SP500",
            "provider": "fred",
            "adjustment": "price_index",
            "return_semantics": "price_index",
            "frequency": "daily",
            "units": "index_points",
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
            "session_date": f"2026-09-{i:02d}",
            "close": str(100 + i),
            "fast_sma": None,
            "slow_sma": None,
            "signal_after_close": None,
        }
        for i in range(1, 22)
    ]

    def digest(value):
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

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
    assert identifier == created["id"] and selected == request() and attempt == 1
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


def test_filtered_job_history_counts_all_matches_before_pagination(tmp_path):
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite")
    running = ledger.enqueue(request())["id"]
    ledger.claim_next()
    assert ledger.list(status="running")["items"][0]["id"] == running
    assert ledger.cancel(running) == "cancel_requested"
    cancelled = ledger.enqueue(request())["id"]
    assert ledger.cancel(cancelled) == "cancelled"
    queued = [ledger.enqueue(request())["id"] for _ in range(12)]
    assert ledger.list(limit=10, status="queued")["total"] == 12
    assert len(ledger.list(limit=10, status="queued")["items"]) == 10
    assert len(ledger.list(limit=10, offset=10, status="queued")["items"]) == 2
    assert ledger.list(status="cancel_requested")["items"][0]["id"] == running
    assert ledger.list(status="running") == {"total": 0, "items": []}
    assert ledger.list(status="cancelled")["items"][0]["id"] == cancelled
    assert ledger.list(status="completed") == {"total": 0, "items": []}
    assert ledger.list()["total"] == 14
    assert queued[-1] in [x["id"] for x in ledger.list(limit=20, status="queued")["items"]]
    with pytest.raises(ModelJobStoreError, match="status"):
        ledger.list(status="all; DROP TABLE model_jobs")


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


# Exact schema used by releases that store only legacy SMA jobs.
_LEGACY_DDL = """
CREATE TABLE model_jobs (
    id TEXT PRIMARY KEY NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled')),
    model_id TEXT NOT NULL CHECK(model_id = 'builtin.sma_crossover'),
    model_version TEXT NOT NULL CHECK(model_version = '1.0.0'),
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    result_json TEXT,
    output_sha256 TEXT,
    source_snapshot_sha256 TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX model_jobs_recent ON model_jobs(created_at DESC, id DESC);
CREATE INDEX model_jobs_pending ON model_jobs(state, created_at, id);
PRAGMA user_version = 1;
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _legacy_database(path):
    sma = _json(request().model_dump(mode="json"))
    digest = hashlib.sha256(sma.encode("utf-8")).hexdigest()
    output = result()
    ids = {}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(_LEGACY_DDL)
        for state in ("queued", "running", "completed", "failed", "cancelled"):
            ids[state] = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO model_jobs (id,state,model_id,model_version,request_json,"
                "request_sha256,attempts,progress,cancel_requested,result_json,output_sha256,"
                "source_snapshot_sha256,error_code,created_at,updated_at,started_at,finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ids[state],
                    state,
                    "builtin.sma_crossover",
                    "1.0.0",
                    sma,
                    digest,
                    0 if state == "queued" else 2,
                    {"running": 12, "completed": 100}.get(state, 0),
                    int(state in {"running", "cancelled"}),
                    _json(output) if state == "completed" else None,
                    output["output_sha256"] if state == "completed" else None,
                    "a" * 64 if state == "completed" else None,
                    "interrupted" if state == "failed" else None,
                    "2026-10-02T00:00:00+00:00",
                    "2026-10-02T01:00:00+00:00",
                    None if state == "queued" else "2026-10-02T00:10:00+00:00",
                    "2026-10-02T00:59:00+00:00"
                    if state in {"completed", "failed", "cancelled"}
                    else None,
                ),
            )
    return ids


def _schema_state(path):
    with closing(sqlite3.connect(path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        indexes = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        rows = conn.execute("SELECT * FROM model_jobs ORDER BY id").fetchall()
    return version, indexes, rows


def test_v1_upgrade_preserves_every_legacy_column_and_existing_lifecycle(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    ids = _legacy_database(path)
    path.chmod(0o600)
    old_mode = stat.S_IMODE(path.stat().st_mode)
    version, indexes, old_rows = _schema_state(path)
    ledger = ModelJobLedger(path)
    assert version == 1
    assert ledger.list()["total"] == 5
    assert ledger.get(ids["completed"], include_result=True)["result"] == result()
    assert ledger.list(analysis_only=True) == {"total": 0, "items": []}
    assert ledger.get(ids["completed"], include_result=True, analysis_only=True) is None
    assert _schema_state(path) == (version, indexes, old_rows)

    assert ledger.cancel("no-such-job") is None  # First write migrates without editing a job.
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == JOB_SCHEMA_VERSION
        legacy_rows = conn.execute(
            f"SELECT {job_store._LEGACY_COLUMNS} FROM model_jobs WHERE request_contract=? ORDER BY id",
            (LEGACY_REQUEST_CONTRACT,),
        ).fetchall()
        assert legacy_rows == old_rows
        assert conn.execute(
            "SELECT count(*) FROM model_jobs WHERE result_envelope_sha256 IS NOT NULL"
        ).fetchone() == (0,)
        assert (
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name"
            ).fetchall()
            == indexes
        )
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert stat.S_IMODE(path.stat().st_mode) == old_mode

    reopened = ModelJobLedger(path)
    assert reopened.get(ids["completed"], include_result=True)["result"] == result()
    assert reopened.reconcile_interrupted() == 1
    assert reopened.get(ids["running"])["state"] == "cancelled"
    assert reopened.claim_next() == (ids["queued"], request(), 1)
    assert reopened.retry(ids["failed"])
    assert reopened.claim_next() == (ids["failed"], request(), 3)


def test_interrupted_schema_swap_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    path = tmp_path / "jobs.sqlite3"
    ids = _legacy_database(path)
    before = _schema_state(path)
    migrate = ModelJobLedger._migrate_legacy

    def interrupted(conn):
        migrate(conn)
        raise sqlite3.OperationalError("synthetic interruption after table swap")

    with monkeypatch.context() as patch:
        patch.setattr(ModelJobLedger, "_migrate_legacy", staticmethod(interrupted))
        with pytest.raises(ModelJobStoreError, match="storage is unavailable"):
            ModelJobLedger(path).enqueue(request())
    assert _schema_state(path) == before
    with closing(sqlite3.connect(path)) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='model_jobs_v2'").fetchone()
            is None
        )

    ledger = ModelJobLedger(path)
    assert ledger.enqueue(request())["state"] == "queued"
    assert ledger.get(ids["completed"], include_result=True)["result"] == result()


def test_future_ledger_version_is_refused_without_edits(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    _legacy_database(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 99")
    before = _schema_state(path)
    ledger = ModelJobLedger(path)
    with pytest.raises(ModelJobStoreError, match="storage is unavailable"):
        ledger.list()
    with pytest.raises(ModelJobStoreError, match="storage is unavailable"):
        ledger.enqueue(request())
    assert _schema_state(path) == before


@pytest.fixture
def analysis_fixture(tmp_path):
    market = tmp_path / "market.sqlite3"
    start = date(2024, 1, 2)
    end = date(2024, 1, 5)
    with closing(_write_connection(market)) as conn, conn:
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES (?,'fred',?,?,'2025-01-01T00:00:00+00:00','synthetic fixture')",
            (
                (code, session.isoformat(), price)
                for code, prices in (("SP500", ("100", "110")), ("NDX", ("100", "90")))
                for session, price in zip((start, end), prices, strict=True)
            ),
        )
        pins = []
        for code in ("SP500", "NDX"):
            _, _, provenance = _read_series(
                conn, kind="index", code=code, provider="fred", start=start, end=end
            )
            identity = next(source for source in PRICE_INDEX_SOURCES if source.code == code)
            pins.append(SourcePin(identity=identity, snapshot_sha256=provenance["snapshot_sha256"]))
    spec = AnalysisRequest(
        model_id=RISK_MODEL_ID,
        model_version="1.0.0",
        sources=tuple(pins),
        window=DateRange(start=start, end=end),
        quality_policy="allow_unverified",
    )
    return market, spec


def _insert_analysis_attempt(ledger, spec, *, state="running"):
    # AL-05A deliberately has no production analysis enqueue or claim path.
    canonical, digest = analysis_job_request_payload(spec)
    identifier = str(uuid.uuid4())
    conn = ledger._open(writable=True)
    assert conn is not None
    with closing(conn), conn:
        conn.execute(
            "INSERT INTO model_jobs (id,state,model_id,model_version,request_contract,"
            "request_json,request_sha256,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                state,
                spec.model_id,
                spec.model_version,
                ANALYSIS_REQUEST_CONTRACT,
                canonical,
                digest,
                1 if state == "running" else 0,
                "2026-10-02T00:00:00+00:00",
                "2026-10-02T00:00:00+00:00",
            ),
        )
    return identifier


def test_analysis_request_canonical_hash_resolves_defaults_and_keeps_source_order(analysis_fixture):
    _, spec = analysis_fixture
    canonical, digest = analysis_job_request_payload(spec)
    assert len(digest) == 64
    assert json.loads(canonical)["parameters"] == {"risk_window": 63, "tail_window": 252}
    explicit = spec.model_copy(update={"parameters": {"tail_window": 252, "risk_window": 63}})
    assert analysis_job_request_payload(explicit) == (canonical, digest)
    reversed_sources = spec.model_copy(update={"sources": spec.sources[::-1]})
    assert analysis_job_request_payload(reversed_sources)[1] != digest


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_id": "builtin.made_up"},
        {"model_id": "builtin.sma_crossover"},
        {"model_version": "2.0.0"},
        {"contract_version": "analysis-model-request-v2"},
        {"parameters": {"tail_window": "252"}},
        {"parameters": {"module": "unapproved"}},
        {"parameters": {"file": "/private/credentials"}},
        {"parameters": {"url": "https://example.invalid"}},
        {"sources": []},
    ],
)
def test_unapproved_or_malformed_analysis_requests_fail_closed(analysis_fixture, overrides):
    _, spec = analysis_fixture
    with pytest.raises(ModelJobStoreError, match="Invalid analysis model-job request"):
        analysis_job_request_payload(spec.model_copy(update=overrides))


@pytest.mark.parametrize("bad_pin", ["unsupported_source", "missing_digest", "malformed_digest"])
def test_job_payload_revalidates_each_source_pin(analysis_fixture, bad_pin):
    _, spec = analysis_fixture
    pin = spec.sources[0]
    if bad_pin == "unsupported_source":
        modified_pin = pin.model_copy(
            update={"identity": pin.identity.model_copy(update={"code": "VIX"})}
        )
    elif bad_pin == "missing_digest":
        modified_pin = {"identity": pin.identity}
    else:
        modified_pin = pin.model_copy(update={"snapshot_sha256": "bad"})
    with pytest.raises(ModelJobStoreError, match="Invalid analysis model-job request"):
        analysis_job_request_payload(spec.model_copy(update={"sources": (modified_pin,)}))


def test_analysis_result_is_attempt_fenced_and_readable_after_market_revision(
    tmp_path, analysis_fixture, monkeypatch
):
    market, spec = analysis_fixture
    job_db = tmp_path / "jobs.sqlite3"
    ledger = ModelJobLedger(job_db)
    identifier = _insert_analysis_attempt(ledger, spec)
    output = evaluate_risk_model(spec, db=market)
    assert not ledger.complete_analysis(identifier, 2, output)
    assert not ledger.complete(identifier, 1, result())
    assert ledger.complete_analysis(identifier, 1, output)
    assert not ledger.complete_analysis(identifier, 1, output)
    with closing(_write_connection(market)) as conn, conn:
        conn.execute("UPDATE index_daily_closes SET close='50' WHERE index_code='SP500'")
    monkeypatch.setenv("FININTEL_DB_PATH", str(market))
    historical = ModelJobLedger(job_db).get(identifier, include_result=True)
    assert historical["result"] == output.model_dump(mode="json")
    assert historical["output_sha256"] == output.output_sha256
    assert historical["source_snapshot_sha256"] is None
    assert ModelJobLedger(job_db).get(identifier, legacy_only=True) is None


def test_analysis_completion_rejects_malformed_mismatched_or_oversized_output(
    tmp_path, analysis_fixture, monkeypatch
):
    market, spec = analysis_fixture
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite3")
    identifier = _insert_analysis_attempt(ledger, spec)
    reversed_sources = spec.model_copy(update={"sources": spec.sources[::-1]})
    with pytest.raises(ModelJobStoreError, match="Invalid analysis model-job result"):
        ledger.complete_analysis(identifier, 1, evaluate_risk_model(reversed_sources, db=market))
    output = evaluate_risk_model(spec, db=market)
    for bad_fields in (
        {"contract_version": "analysis-model-result-v2"},
        {"output_sha256": "f" * 64},
        {"points": (output.points[0] | {"unexpected": True}, *output.points[1:])},
    ):
        with pytest.raises(ModelJobStoreError, match="Invalid analysis model-job result"):
            ledger.complete_analysis(identifier, 1, output.model_copy(update=bad_fields))
    with monkeypatch.context() as patch:
        patch.setattr(job_store, "MAX_RESULT_BYTES", 32)
        with pytest.raises(ModelJobStoreError, match="Invalid analysis model-job result"):
            ledger.complete_analysis(identifier, 1, output)
    assert ledger.get(identifier)["state"] == "running"
    assert ledger.cancel(identifier) == "cancel_requested"
    assert not ledger.complete_analysis(identifier, 1, output)


@pytest.mark.parametrize(
    "column",
    ["request_sha256", "output_sha256", "request_json", "result_json", "result_envelope_sha256"],
)
def test_analysis_read_fails_closed_on_stored_corruption(tmp_path, analysis_fixture, column):
    market, spec = analysis_fixture
    path = tmp_path / "jobs.sqlite3"
    ledger = ModelJobLedger(path)
    identifier = _insert_analysis_attempt(ledger, spec)
    assert ledger.complete_analysis(identifier, 1, evaluate_risk_model(spec, db=market))
    # Column names are the fixed cases above.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"UPDATE model_jobs SET {column}=? WHERE id=?", ("0" * 64, identifier))
    with pytest.raises(ModelJobStoreError, match="Stored model-job result failed integrity checks"):
        ledger.get(identifier, include_result=True)


def test_legacy_queue_entry_point_does_not_accept_analysis_requests(tmp_path, analysis_fixture):
    _, spec = analysis_fixture
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite3")
    with pytest.raises(ModelJobStoreError, match="Invalid typed model job request"):
        ledger.enqueue(spec)
    identifier = _insert_analysis_attempt(ledger, spec, state="queued")
    assert ledger.cancel(identifier, legacy_only=True) is None
    assert ledger.list(legacy_only=True) == {"total": 0, "items": []}
    assert ledger.cancel(identifier) == "cancelled"
    assert not ledger.retry(identifier)
    legacy = ledger.enqueue(request())
    assert ledger.claim_next() == (legacy["id"], request(), 1)


@pytest.mark.parametrize(
    ("model_id", "source_count"),
    [
        ("builtin.ema_trend", 1),
        ("builtin.momentum", 1),
        ("builtin.relative_strength", 2),
        ("builtin.rsi_state", 1),
        ("builtin.risk_profile", 2),
        ("builtin.market_regime", 1),
    ],
)
def test_each_approved_analysis_identity_fits_the_ledger(
    tmp_path, analysis_fixture, model_id, source_count
):
    _, spec = analysis_fixture
    spec = spec.model_copy(update={"model_id": model_id, "sources": spec.sources[:source_count]})
    ledger = ModelJobLedger(tmp_path / "jobs.sqlite3")
    identifier = _insert_analysis_attempt(ledger, spec)
    assert ledger.get(identifier)["model_id"] == model_id
    claim = ledger.read_claim(identifier, 1)
    assert claim is not None and claim.model_id == model_id


def test_legacy_managed_routes_exclude_internal_analysis_jobs(
    tmp_path, analysis_fixture, monkeypatch
):
    _, spec = analysis_fixture
    path = tmp_path / "jobs.sqlite3"
    ledger = ModelJobLedger(path)
    internal = _insert_analysis_attempt(ledger, spec, state="queued")
    legacy = ledger.enqueue(request())
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "synthetic-job-token")
    monkeypatch.setenv("FININTEL_MODEL_JOBS_DB_PATH", str(path))
    client = TestClient(app, headers={"x-finintel-process-token": "synthetic-job-token"})
    response = client.get("/api/v1/models/jobs")
    assert response.status_code == 200
    assert response.json() == {"total": 1, "items": [legacy]}
    item_url = f"/api/v1/models/jobs/{internal}"
    assert client.get(item_url).status_code == 404
    assert client.post(f"{item_url}/cancel").status_code == 404
    assert client.post(f"{item_url}/retry").status_code == 404
    assert ledger.get(internal)["state"] == "queued"


def test_analysis_read_rejects_a_valid_timestamp_only_mutation(tmp_path, analysis_fixture):
    market, spec = analysis_fixture
    path = tmp_path / "jobs.sqlite3"
    ledger = ModelJobLedger(path)
    identifier = _insert_analysis_attempt(ledger, spec)
    output = evaluate_risk_model(spec, db=market)
    assert ledger.complete_analysis(identifier, 1, output)

    with closing(sqlite3.connect(path)) as conn, conn:
        raw, digest = conn.execute(
            "SELECT result_json,result_envelope_sha256 FROM model_jobs WHERE id=?",
            (identifier,),
        ).fetchone()
        assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        payload = json.loads(raw)
        payload["evaluated_at_utc"] = "2025-01-01T00:00:00Z"
        changed = _json(payload)
        reparsed = AnalysisResult.model_validate_json(changed)
        assert reparsed.evaluated_at_utc != output.evaluated_at_utc
        assert reparsed.output_sha256 == output.output_sha256
        assert reparsed.reproducibility_sha256 == output.reproducibility_sha256
        conn.execute("UPDATE model_jobs SET result_json=? WHERE id=?", (changed, identifier))
    with pytest.raises(ModelJobStoreError, match="Stored model-job result failed integrity checks"):
        ledger.get(identifier, include_result=True)


def test_v2_upgrade_preserves_analysis_results_and_rolls_back_on_interruption(
    tmp_path, analysis_fixture, monkeypatch
):
    market, spec = analysis_fixture
    path = tmp_path / "jobs.sqlite3"
    ledger = ModelJobLedger(path)
    legacy = ledger.enqueue(request())
    analysis = _insert_analysis_attempt(ledger, spec)
    report = evaluate_risk_model(spec, db=market)
    assert ledger.complete_analysis(analysis, 1, report)
    with closing(sqlite3.connect(path)) as conn, conn:
        # Recreate the previous on-disk constraints and retain every column.
        columns = job_store._V2_COLUMNS
        ddl = job_store._TABLE_DDL.replace(", 'backtest-job-request-v1'", "")
        conn.execute(ddl.format(table="previous_model_jobs"))
        conn.execute(
            f"INSERT INTO previous_model_jobs ({columns}) SELECT {columns} FROM model_jobs"
        )
        conn.execute("DROP TABLE model_jobs")
        conn.execute("ALTER TABLE previous_model_jobs RENAME TO model_jobs")
        for statement in job_store._JOB_INDEX_DDL:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=2")
    before = _schema_state(path)
    assert ledger.get(analysis, include_result=True)["result"] == report.model_dump(mode="json")
    assert ledger.list(backtest_only=True) == {"total": 0, "items": []}

    migrate = ModelJobLedger._migrate_table

    def interrupted(conn, columns):
        migrate(conn, columns)
        raise sqlite3.OperationalError("synthetic migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(ModelJobLedger, "_migrate_table", staticmethod(interrupted))
        with pytest.raises(ModelJobStoreError, match="storage is unavailable"):
            ledger.reconcile_interrupted()
    assert _schema_state(path) == before
    assert ModelJobLedger(path).reconcile_interrupted() == 0
    assert _schema_state(path) == (JOB_SCHEMA_VERSION, before[1], before[2])
    assert ledger.get(legacy["id"], legacy_only=True) == legacy
    assert ledger.get(analysis, include_result=True)["result"] == report.model_dump(mode="json")
