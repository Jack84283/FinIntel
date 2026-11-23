"""Explicit dispatch and source checks for durable Analysis Library jobs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from finintel.market_data.analytics import AnalyticsError, shared_session_dates
from finintel.models.contracts import AnalysisRequest, AnalysisResult
from finintel.models.evaluation import (
    PinnedInput,
    read_pinned_inputs,
    read_pinned_inputs_on_connection,
)
from finintel.models.managed_approvals import MANAGED_ANALYSIS_REVISIONS
from finintel.models.regime import evaluate_regime_model
from finintel.models.registry import (
    MAX_MODEL_OBSERVATIONS,
    REGIME_MODEL_ID,
    RISK_MODEL_ID,
    get_model_definition,
)
from finintel.models.risk import evaluate_risk_model
from finintel.models.trend import evaluate_trend_model
from finintel.storage.db import StorageError, _ensure_version, database_path

# Dispatch selects only built-in evaluator functions after version and revision approval.
_MANAGED_EVALUATORS = MappingProxyType(
    {
        ("builtin.ema_trend", "1.0.0"): evaluate_trend_model,
        ("builtin.momentum", "1.0.0"): evaluate_trend_model,
        ("builtin.relative_strength", "1.0.0"): evaluate_trend_model,
        ("builtin.rsi_state", "1.0.0"): evaluate_trend_model,
        (RISK_MODEL_ID, "1.0.0"): evaluate_risk_model,
        (REGIME_MODEL_ID, "1.0.0"): evaluate_regime_model,
    }
)


# Relative Strength can select all four eligible series. Each evaluator independently
# limits its result to at most 2,000 rows and the existing 4 MiB output quota.
MAX_AGGREGATE_OBSERVATIONS = 4 * MAX_MODEL_OBSERVATIONS
SOURCE_COMMIT_LOCK_TIMEOUT_SECONDS = 1.0


def _checked_request(request: AnalysisRequest) -> AnalysisRequest:
    request = AnalysisRequest.model_validate_json(request.model_dump_json())
    key = (request.model_id, request.model_version)
    revision = MANAGED_ANALYSIS_REVISIONS.get(key)
    if revision is None or key not in _MANAGED_EVALUATORS:
        raise AnalyticsError("Unsupported managed analysis model or version")
    definition = get_model_definition(request)
    if definition.metadata.implementation_revision != revision:
        raise AnalyticsError("Unsupported managed analysis implementation revision")
    definition.validate_request(request)
    if len(request.sources) * definition.metadata.observations.maximum > MAX_AGGREGATE_OBSERVATIONS:
        raise AnalyticsError("Managed analysis exceeds the aggregate observation limit")
    return request


def _observation_limit(request: AnalysisRequest) -> int:
    return min(get_model_definition(request).metadata.observations.maximum, MAX_MODEL_OBSERVATIONS)


def _validate_history(request: AnalysisRequest, inputs: list[PinnedInput]) -> None:
    definition = get_model_definition(request)
    minimum = (
        definition.parameter_minimum(definition.validate_request(request))
        if definition.parameter_minimum is not None
        else definition.metadata.observations.minimum
    )
    if len(shared_session_dates([source.rows for source in inputs])) < minimum:
        raise AnalyticsError(f"At least {minimum} shared observed sessions are required")


def preflight_analysis_job(request: AnalysisRequest, *, db: Path | None = None) -> None:
    """Validate the ordered local source pins before queuing, without calculations."""
    request = _checked_request(request)
    inputs = read_pinned_inputs(request, db, _observation_limit(request), family="Managed analysis")
    _validate_history(request, inputs)


def evaluate_analysis_job(request: AnalysisRequest, *, db: Path | None = None) -> AnalysisResult:
    """Run the selected local evaluator in its ordinary read-only snapshot."""
    request = _checked_request(request)
    evaluator = _MANAGED_EVALUATORS[(request.model_id, request.model_version)]
    # The evaluator reads and validates source identity, pins and quality before calculation.
    result = evaluator(request, db=db)
    get_model_definition(request).validate_result(request, result)
    return result


@contextmanager
def reserve_source_for_commit() -> Iterator[sqlite3.Connection]:
    """Exclude market SQLite writers until the job ledger commits evidence."""
    conn = None
    try:
        # mode=rw permits a reserved writer slot without creating a database. Take
        # the market lock before the ledger lock and release it on every exit.
        try:
            path = database_path().resolve(strict=True)
            conn = sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, timeout=SOURCE_COMMIT_LOCK_TIMEOUT_SECONDS
            )
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA query_only = ON")
            _ensure_version(conn, allow_missing=False)
        except (OSError, sqlite3.Error) as exc:
            raise StorageError("Local source lock unavailable for analysis commit") from exc

        yield conn
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def guard_analysis_source_for_commit(
    request: AnalysisRequest, result: AnalysisResult
) -> Iterator[None]:
    with reserve_source_for_commit() as conn:
        current = read_pinned_inputs_on_connection(
            request, conn, _observation_limit(request), family="Managed analysis"
        )
        if tuple(source.quality for source in current) != tuple(
            source.quality for source in result.evidence.sources
        ):
            raise AnalyticsError("Local source quality changed during managed analysis")
        yield
