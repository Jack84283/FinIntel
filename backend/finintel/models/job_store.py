"""Issue #32: opt-in, local-only durable research-job ledger.

Phase 2A stores *metadata and results* but intentionally does not execute jobs,
expose HTTP routes or start a worker. Execution must live in an isolated follow-up
worker. The market-data database and its schema are never modified here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from finintel.market_data.analytics import CALCULATION_VERSION
from finintel.models.api import ModelResearchResult
from finintel.models.registry import (
    IMPLEMENTATION_REVISION,
    MAX_MODEL_OBSERVATIONS,
    MODEL_ID,
    MODEL_VERSION,
    ModelParameters,
)
from finintel.storage.db import database_path

JOB_SCHEMA_VERSION = 1
MAX_ACTIVE_JOBS = 32
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 3
MAX_LIST_LIMIT = 100

STATES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
ERROR_CODES = frozenset(
    {
        "interrupted",
        "invalid_input",
        "quality_blocked",
        "source_changed",
        "storage_unavailable",
        "resource_limit",
        "worker_failure",
    }
)


class ModelJobStoreError(ValueError):
    """A safe error that does not contain private inputs or file paths."""


class ModelJobRequest(BaseModel):
    """Only the built-in indicator is schedulable; no executable paths or code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: Literal["builtin.sma_crossover"] = MODEL_ID
    model_version: Literal["1.0.0"] = MODEL_VERSION
    code: Literal["SP500", "DJIA", "NASDAQCOM", "NDX"] = "SP500"
    start: date
    end: date
    quality_policy: Literal["require_complete", "allow_unverified"] = "require_complete"
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    # Durable jobs require source pinning before queuing; synchronous evaluation remains optional.
    expected_snapshot_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_range(self) -> ModelJobRequest:
        if self.start > self.end or self.end > datetime.now(UTC).date():
            raise ValueError("Invalid model job date range")
        return self


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def jobs_database_path() -> Path:
    """Keep job state separate from the (possibly huge) price-observation database."""
    override = os.environ.get("FININTEL_MODEL_JOBS_DB_PATH")
    return (
        Path(override).expanduser()
        if override
        else database_path().with_name("finintel-model-jobs.sqlite3")
    )


DDL = """
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
"""


class ModelJobLedger:
    """SQLite-backed, atomic job transitions; no network or execution hooks."""

    def __init__(self, db: Path | None = None):
        self.path = db if db is not None else jobs_database_path()

    def _open(self, *, writable: bool) -> sqlite3.Connection | None:
        path = self.path
        if path.is_symlink() or path.resolve() == database_path().resolve():
            raise ModelJobStoreError("Unsafe model-job storage location")
        if not writable and not path.is_file():
            return None
        if writable:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        fd = os.open(path, flags, 0o600)
                        os.close(fd)
                    except FileExistsError:
                        pass
                if path.is_symlink():
                    raise ModelJobStoreError("Unsafe model-job storage location")
            except OSError as exc:
                raise ModelJobStoreError("Model-job storage is unavailable") from exc
        try:
            if writable:
                conn = sqlite3.connect(path, timeout=10)
            else:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 10000")
            if writable:
                self._initialize(conn)
            else:
                conn.execute("PRAGMA query_only = ON")
                if conn.execute("PRAGMA user_version").fetchone()[0] != JOB_SCHEMA_VERSION:
                    raise ModelJobStoreError("Model-job storage needs initialization or upgrade")
            return conn
        except (sqlite3.Error, ModelJobStoreError) as exc:
            if "conn" in locals():
                conn.close()
            raise ModelJobStoreError("Model-job storage is unavailable") from exc

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == JOB_SCHEMA_VERSION:
            return
        if (
            version != 0
            or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
        ):
            raise ModelJobStoreError("Unsupported model-job storage version")
        # executescript commits implicitly; creation happens only for a new empty database.
        conn.executescript("BEGIN IMMEDIATE;\n" + DDL + "\nPRAGMA user_version = 1;\nCOMMIT;")

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        summary = {
            key: row[key]
            for key in (
                "id",
                "state",
                "model_id",
                "model_version",
                "request_sha256",
                "attempts",
                "progress",
                "cancel_requested",
                "output_sha256",
                "source_snapshot_sha256",
                "error_code",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
            )
        }
        summary["status"] = (
            "cancel_requested"
            if row["state"] == "running" and row["cancel_requested"]
            else row["state"]
        )
        return summary

    def enqueue(self, request: ModelJobRequest) -> dict:
        if not isinstance(request, ModelJobRequest):
            raise ModelJobStoreError("Invalid typed model job request")
        spec = request.model_dump(mode="json")
        canonical = _canonical(spec)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        identifier = str(uuid.uuid4())
        at = _now()
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    "SELECT COUNT(*) FROM model_jobs WHERE state IN ('queued','running')"
                ).fetchone()[0]
                if active >= MAX_ACTIVE_JOBS:
                    raise ModelJobStoreError("Model-job queue is full")
                conn.execute(
                    "INSERT INTO model_jobs "
                    "(id,state,model_id,model_version,request_json,request_sha256,"
                    "created_at,updated_at) "
                    "VALUES (?,'queued',?,?,?,?,?,?)",
                    (identifier, MODEL_ID, MODEL_VERSION, canonical, digest, at, at),
                )
                row = conn.execute("SELECT * FROM model_jobs WHERE id=?", (identifier,)).fetchone()
            return self._summary(row)
        finally:
            conn.close()

    @staticmethod
    def _verified_stored_result(row: sqlite3.Row) -> dict | None:
        """Fail closed on stored-result corruption, without modifying any observation."""
        raw = row["result_json"]
        if row["state"] != "completed":
            if raw is not None:
                raise ModelJobStoreError("Stored model-job result failed integrity checks")
            return None
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ModelJobStoreError("Stored model-job result failed integrity checks")
        try:
            result = ModelResearchResult.model_validate_json(raw)
            original = ModelJobRequest.model_validate_json(row["request_json"]).model_dump(
                mode="json"
            )
            lineage = result.lineage
            points = [point.model_dump() for point in result.points]
            identity = {
                "instrument_kind": "index",
                "code": original["code"],
                "provider": "fred",
                "adjustment": "price_index",
                "return_semantics": "price_index",
                "frequency": "daily",
                "units": "index_points",
            }
            valid = (
                json.loads(raw) == result.model_dump()
                and hashlib.sha256(row["request_json"].encode("utf-8")).hexdigest()
                == row["request_sha256"]
                and original["model_id"] == row["model_id"]
                and original["model_version"] == row["model_version"]
                and result.execution_contract == "model-research-evaluation-v1"
                and result.status == "completed"
                and result.read_only is True
                and result.no_network is True
                and result.no_synthetic_sessions is True
                and result.signal_after_close_only is True
                and result.backtest_validated is False
                and result.persistent_job is False
                and isinstance(lineage, dict)
                and lineage.get("model_id") == row["model_id"]
                and lineage.get("model_version") == row["model_version"]
                and lineage.get("implementation_revision") == IMPLEMENTATION_REVISION
                and lineage.get("analysis_calculation_version") == CALCULATION_VERSION
                and lineage.get("source_identity") == identity
                and lineage.get("parameters") == original["parameters"]
                and lineage.get("requested_start") == original["start"]
                and lineage.get("requested_end") == original["end"]
                and lineage.get("quality_policy") == original["quality_policy"]
                and lineage.get("source_snapshot_sha256") == original["expected_snapshot_sha256"]
                and lineage.get("point_in_time_membership") == "not_used"
                and lineage.get("dividends_included") is False
                and lineage.get("corporate_action_adjustment") == "none_index_price_level"
                and result.quality_warning
                in (None, "Unverified data: research-only results, not verified for backtesting")
                and 0 < len(points) <= MAX_MODEL_OBSERVATIONS
                and result.observations == len(points)
                and points[0]["session_date"] == lineage.get("first_observed_session")
                and points[-1]["session_date"] == lineage.get("last_observed_session")
                and all(
                    points[i]["session_date"] < points[i + 1]["session_date"]
                    for i in range(len(points) - 1)
                )
                and result.output_sha256 == row["output_sha256"]
                and lineage.get("source_snapshot_sha256") == row["source_snapshot_sha256"]
                and hashlib.sha256(_canonical(points).encode("utf-8")).hexdigest()
                == row["output_sha256"]
                and hashlib.sha256(_canonical(lineage).encode("utf-8")).hexdigest()
                == result.reproducibility_sha256
            )
            if not valid:
                raise ModelJobStoreError("Stored model-job result failed integrity checks")
            return result.model_dump()
        except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ModelJobStoreError("Stored model-job result failed integrity checks") from exc

    def get(self, identifier: str, *, include_result: bool = False) -> dict | None:
        conn = self._open(writable=False)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM model_jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return None
            result = self._summary(row)
            if include_result:
                result["result"] = self._verified_stored_result(row)
            return result
        finally:
            conn.close()

    def list(self, *, limit: int = 20, offset: int = 0) -> dict:
        if not 1 <= limit <= MAX_LIST_LIMIT or not 0 <= offset <= 10_000:
            raise ModelJobStoreError("Invalid model-job pagination")
        conn = self._open(writable=False)
        if conn is None:
            return {"total": 0, "items": []}
        try:
            total = conn.execute("SELECT COUNT(*) FROM model_jobs").fetchone()[0]
            items = conn.execute(
                "SELECT * FROM model_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {"total": total, "items": [self._summary(row) for row in items]}
        finally:
            conn.close()

    @staticmethod
    def _validate_attempt(attempt: int) -> None:
        if type(attempt) is not int or not 1 <= attempt <= MAX_ATTEMPTS:
            raise ModelJobStoreError("Invalid model-job attempt")

    def claim_next(self) -> tuple[str, ModelJobRequest, int] | None:
        """Reserve one queued job atomically; return a monotonic attempt generation."""
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id,request_json,attempts FROM model_jobs "
                    "WHERE state='queued' ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                if row["attempts"] >= MAX_ATTEMPTS:
                    raise ModelJobStoreError("Model-job attempt quota exhausted")
                conn.execute(
                    "UPDATE model_jobs SET state='running',attempts=attempts+1,"
                    "started_at=?,updated_at=?,progress=0 WHERE id=? AND state='queued'",
                    (_now(), _now(), row["id"]),
                )
                request = ModelJobRequest.model_validate_json(row["request_json"])
                return row["id"], request, row["attempts"] + 1
        finally:
            conn.close()

    def set_progress(self, identifier: str, attempt: int, progress: int) -> bool:
        self._validate_attempt(attempt)
        if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress < 100:
            raise ModelJobStoreError("Invalid model-job progress")
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE model_jobs SET progress=?,updated_at=? "
                    "WHERE id=? AND attempts=? AND state='running' "
                    "AND cancel_requested=0 AND progress<=?",
                    (progress, _now(), identifier, attempt, progress),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    def cancel(self, identifier: str) -> str | None:
        """Queued cancellation is final; running cancellation is cooperative."""
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state FROM model_jobs WHERE id=?", (identifier,)
                ).fetchone()
                if row is None:
                    return None
                if row["state"] == "queued":
                    at = _now()
                    conn.execute(
                        "UPDATE model_jobs SET state='cancelled',cancel_requested=1,"
                        "finished_at=?,updated_at=? WHERE id=?",
                        (at, at, identifier),
                    )
                    return "cancelled"
                if row["state"] == "running":
                    conn.execute(
                        "UPDATE model_jobs SET cancel_requested=1,updated_at=? WHERE id=?",
                        (_now(), identifier),
                    )
                    return "cancel_requested"
                return row["state"]
        finally:
            conn.close()

    def finish_cancelled(self, identifier: str, attempt: int) -> bool:
        """Cooperative worker acknowledgement; no results survive cancellation."""
        self._validate_attempt(attempt)
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                at = _now()
                cursor = conn.execute(
                    "UPDATE model_jobs SET state='cancelled',updated_at=?,finished_at=? "
                    "WHERE id=? AND attempts=? AND state='running' AND cancel_requested=1",
                    (at, at, identifier, attempt),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    def complete(self, identifier: str, attempt: int, result: dict) -> bool:
        """Persist only the complete allowlisted indicator result of this claim."""
        self._validate_attempt(attempt)
        try:
            validated = ModelResearchResult.model_validate(result)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ModelJobStoreError("Invalid model-job output schema") from exc
        if (
            validated.execution_contract != "model-research-evaluation-v1"
            or validated.status != "completed"
            or validated.backtest_validated is not False
            or validated.persistent_job is not False
            or validated.read_only is not True
            or validated.no_network is not True
            or validated.no_synthetic_sessions is not True
            or validated.signal_after_close_only is not True
        ):
            raise ModelJobStoreError("Invalid model-job output contract")
        lineage = validated.lineage
        # The API result model has a dict lineage for backwards compatibility;
        # this private ledger must never retain arbitrary hidden fields inside it.
        expected_lineage_keys = {
            "model_id",
            "model_version",
            "implementation_revision",
            "parameters",
            "source_identity",
            "source_snapshot_sha256",
            "analysis_calculation_version",
            "requested_start",
            "requested_end",
            "first_observed_session",
            "last_observed_session",
            "quality_policy",
            "quality_status",
            "quality_verified",
            "point_in_time_membership",
            "dividends_included",
            "corporate_action_adjustment",
        }
        if not isinstance(lineage, dict) or set(lineage) != expected_lineage_keys:
            raise ModelJobStoreError("Invalid model-job lineage schema")
        if (
            lineage["model_id"] != MODEL_ID
            or lineage["model_version"] != MODEL_VERSION
            or lineage["implementation_revision"] != IMPLEMENTATION_REVISION
            or lineage["analysis_calculation_version"] != CALCULATION_VERSION
            or lineage["point_in_time_membership"] != "not_used"
            or lineage["dividends_included"] is not False
            or lineage["corporate_action_adjustment"] != "none_index_price_level"
            or type(lineage["quality_verified"]) is not bool
            or not isinstance(lineage["quality_status"], str)
            or len(lineage["quality_status"]) > 40
            or not lineage["quality_status"].replace("_", "").isalnum()
        ):
            raise ModelJobStoreError("Incompatible model-job lineage")
        source = lineage["source_snapshot_sha256"]
        output = validated.output_sha256
        reproducibility = validated.reproducibility_sha256
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in (source, output, reproducibility)
        ):
            raise ModelJobStoreError("Invalid model-job source/output hash")
        if (
            validated.observations != len(validated.points)
            or not 0 < validated.observations <= MAX_MODEL_OBSERVATIONS
            or validated.quality_warning
            not in (
                None,
                "Unverified data: research-only results, not verified for backtesting",
            )
        ):
            raise ModelJobStoreError("Invalid model-job observations or warning")
        points = [point.model_dump() for point in validated.points]
        if (
            points[0]["session_date"] != lineage["first_observed_session"]
            or points[-1]["session_date"] != lineage["last_observed_session"]
            or any(
                points[i]["session_date"] >= points[i + 1]["session_date"]
                for i in range(len(points) - 1)
            )
            or hashlib.sha256(_canonical(points).encode("utf-8")).hexdigest() != output
            or hashlib.sha256(_canonical(lineage).encode("utf-8")).hexdigest() != reproducibility
        ):
            raise ModelJobStoreError("Inconsistent model-job output hashes or dates")
        canonical = _canonical(validated.model_dump())
        if len(canonical.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ModelJobStoreError("Model-job result exceeds retention quota")
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT request_json,state,cancel_requested,attempts "
                    "FROM model_jobs WHERE id=?",
                    (identifier,),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "running"
                    or row["cancel_requested"]
                    or row["attempts"] != attempt
                ):
                    return False
                original = json.loads(row["request_json"])
                source_identity = lineage["source_identity"]
                expected_identity = {
                    "instrument_kind": "index",
                    "code": original["code"],
                    "provider": "fred",
                    "adjustment": "price_index",
                    "return_semantics": "price_index",
                    "frequency": "daily",
                    "units": "index_points",
                }
                if (
                    source_identity != expected_identity
                    or lineage["requested_start"] != original["start"]
                    or lineage["requested_end"] != original["end"]
                    or lineage["parameters"] != original["parameters"]
                    or lineage["quality_policy"] != original["quality_policy"]
                    or source != original["expected_snapshot_sha256"]
                ):
                    raise ModelJobStoreError("Model-job result does not match queued request")
                at = _now()
                cursor = conn.execute(
                    "UPDATE model_jobs SET state='completed',progress=100,result_json=?,"
                    "output_sha256=?,source_snapshot_sha256=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND attempts=? AND state='running' AND cancel_requested=0",
                    (canonical, output, source, at, at, identifier, attempt),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    def fail(self, identifier: str, attempt: int, code: str) -> bool:
        self._validate_attempt(attempt)
        if code not in ERROR_CODES:
            raise ModelJobStoreError("Invalid safe model-job error code")
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                at = _now()
                cursor = conn.execute(
                    "UPDATE model_jobs SET state='failed',error_code=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND attempts=? AND state='running' AND cancel_requested=0",
                    (code, at, at, identifier, attempt),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    def retry(self, identifier: str) -> bool:
        """Retries retain the *identical* request and its expected source pin."""
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE model_jobs SET state='queued',progress=0,cancel_requested=0,"
                    "result_json=NULL,output_sha256=NULL,source_snapshot_sha256=NULL,"
                    "error_code=NULL,started_at=NULL,finished_at=NULL,updated_at=? "
                    "WHERE id=? AND state IN ('failed','cancelled') AND attempts<?",
                    (_now(), identifier, MAX_ATTEMPTS),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    def reconcile_interrupted(self) -> int:
        """Call before starting a new worker; never auto-resume on a new source."""
        conn = self._open(writable=True)
        assert conn is not None
        try:
            with conn:
                at = _now()
                cancelled = conn.execute(
                    "UPDATE model_jobs SET state='cancelled',updated_at=?,finished_at=? "
                    "WHERE state='running' AND cancel_requested=1",
                    (at, at),
                ).rowcount
                failed = conn.execute(
                    "UPDATE model_jobs SET state='failed',error_code='interrupted',"
                    "updated_at=?,finished_at=? WHERE state='running' AND cancel_requested=0",
                    (at, at),
                ).rowcount
                return cancelled + failed
        finally:
            conn.close()
