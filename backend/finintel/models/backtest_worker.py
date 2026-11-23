"""Read-only source checks and isolated execution of a pinned SMA backtest."""

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_store import _quality_status, _read_series
from finintel.models.backtest import _test_indices, _validated_points, run_sma_backtest
from finintel.models.durable_backtest import (
    BACKTEST_RESULT_CONTRACT,
    DurableBacktestRequest,
    digest,
)
from finintel.models.job_store import ModelJobLedger
from finintel.models.managed_analysis import reserve_source_for_commit
from finintel.models.registry import MAX_MODEL_OBSERVATIONS
from finintel.storage.db import _read_connection, database_path


def _source_job(ledger: ModelJobLedger, request: DurableBacktestRequest) -> dict:
    source = ledger.get(request.source_job_id, include_result=True, legacy_only=True)
    if (
        source is None
        or source["state"] != "completed"
        or source["request_sha256"] != request.source_request_sha256
        or source["output_sha256"] != request.source_output_sha256
        or source["source_snapshot_sha256"] != request.simulation.expected_snapshot_sha256
    ):
        raise AnalyticsError("Source revision does not match the completed SMA job")
    research = source["result"]
    if (
        research["reproducibility_sha256"] != request.source_lineage_sha256
        or research["lineage"]["implementation_revision"] != request.source_implementation_revision
    ):
        raise AnalyticsError("Source revision does not match the completed SMA lineage")
    lineage = research["lineage"]
    if lineage["quality_policy"] != "require_complete" or lineage["quality_verified"] is not True:
        raise AnalyticsError("Backtest source job requires strict verified quality")
    return research


def _check_local(conn: sqlite3.Connection, request: DurableBacktestRequest, research: dict) -> None:
    lineage = research["lineage"]
    simulation = request.simulation
    identity, observations, provenance = _read_series(
        conn,
        kind="index",
        code=simulation.index_code,
        provider="fred",
        start=date.fromisoformat(lineage["requested_start"]),
        end=date.fromisoformat(lineage["requested_end"]),
        max_observations=MAX_MODEL_OBSERVATIONS,
    )
    if (
        identity.adjustment != "price_index"
        or provenance["snapshot_sha256"] != simulation.expected_snapshot_sha256
        or len(observations) != len(research["points"])
        or any(
            day != point["session_date"] or Decimal(close) != Decimal(point["close"])
            for (day, close), point in zip(observations, research["points"], strict=True)
        )
    ):
        raise AnalyticsError("Local source snapshot changed")
    if not _quality_status(conn, [simulation.index_code], precise_scan_status=True)[
        "quality_verified"
    ]:
        raise AnalyticsError("Current complete source quality validation required")


def preflight_backtest(ledger: ModelJobLedger, request: DurableBacktestRequest) -> dict:
    research = _source_job(ledger, request)
    conn = _read_connection(database_path())
    if conn is None:
        raise AnalyticsError("Local source revision unavailable")
    try:
        _check_local(conn, request, research)
    finally:
        conn.close()
    points, dates, _ = _validated_points(research, request.simulation)
    _test_indices(points, dates, request.simulation)
    return research


def execute_backtest(
    ledger: ModelJobLedger, identifier: str, attempt: int, request: DurableBacktestRequest
) -> bool:
    research = preflight_backtest(ledger, request)
    report = run_sma_backtest(research=research, request=request.simulation)
    envelope = {
        "contract_version": BACKTEST_RESULT_CONTRACT,
        "request_sha256": digest(request.model_dump(mode="json")),
        "report": report,
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
    }
    with reserve_source_for_commit() as conn:
        current = _source_job(ledger, request)
        _check_local(conn, request, current)
        return ledger.complete_backtest(identifier, attempt, envelope)
