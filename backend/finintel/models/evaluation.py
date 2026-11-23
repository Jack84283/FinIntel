"""Shared read-only snapshot and result handling for internal Analysis Library evaluators."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path

from finintel.market_data.analytics import AnalyticsError, Observation, validated_observations
from finintel.market_data.analytics_store import _quality_status, _read_series
from finintel.models.contracts import (
    AnalysisEvidence,
    AnalysisRequest,
    AnalysisResult,
    ModelIdentity,
    ObservationRange,
    OutputValue,
    QualityEvidence,
    SourceEvidence,
    SourcePin,
)
from finintel.models.registry import ModelDefinition, StrictModel
from finintel.storage.db import StorageError, _read_connection, database_path

Point = dict[str, OutputValue]
# Bound fixed-point expansion from corrupt local data before formatting, even if
# SQLite contains a short scientific-notation string for a huge exponent.
MAX_DECIMAL_CHARACTERS = 256
MODEL_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class PinnedInput:
    pin: SourcePin
    rows: list[Observation]
    quality: QualityEvidence


def fixed_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    representation = value.as_tuple()
    digits = len(representation.digits)
    places = representation.exponent
    length = max(1, digits + places) + (1 - places if places < 0 else 0)
    if length + representation.sign > MAX_DECIMAL_CHARACTERS:
        raise AnalyticsError("Fixed-point model result exceeds the numeric representation limit")
    return format(value, "f")


def read_pinned_inputs(
    request: AnalysisRequest, db: Path | None, maximum: int, *, family: str
) -> list[PinnedInput]:
    conn = _read_connection(db if db is not None else database_path())
    if conn is None:
        raise AnalyticsError("No local stored observations are available")
    try:
        inputs = []
        for pin in request.sources:
            source = pin.identity
            identity, rows, provenance = _read_series(
                conn,
                kind=source.instrument_kind,
                code=source.code,
                provider=source.provider,
                start=request.window.start,
                end=request.window.end,
                max_observations=maximum,
            )
            if asdict(identity) != source.model_dump():
                raise AnalyticsError("Stored identity does not match the pinned price-index source")
            if provenance["snapshot_sha256"] != pin.snapshot_sha256:
                raise AnalyticsError("Local source snapshot changed; refresh analysis inputs")
            quality = _quality_status(conn, [source.code], precise_scan_status=True)
            if quality["quality_verified"]:
                evidence = QualityEvidence(status="verified")
            elif request.quality_policy == "require_complete":
                raise AnalyticsError("Current complete Data Quality scan required for each source")
            else:
                match quality["status"]:
                    case "not_run":
                        evidence = QualityEvidence(
                            status="not_run", warnings=("quality_scan_not_run",)
                        )
                    case "partial":
                        evidence = QualityEvidence(
                            status="partial", warnings=("quality_scan_partial",)
                        )
                    case "complete":
                        evidence = QualityEvidence(
                            status="blocked", warnings=("unresolved_quality_findings",)
                        )
                    case "stale":
                        evidence = QualityEvidence(status="stale", warnings=("quality_scan_stale",))
                    case _:
                        raise AnalyticsError("Unsupported Data Quality scan status")
            inputs.append(PinnedInput(pin, validated_observations(rows), evidence))
        return inputs
    except sqlite3.Error as exc:
        raise StorageError(f"Local {family.lower()} model query failed") from exc
    finally:
        conn.close()


def build_result(
    request: AnalysisRequest,
    definition: ModelDefinition,
    parameters: StrictModel,
    inputs: list[PinnedInput],
    sessions: list[date],
    points: tuple[Point, ...],
    evaluated_at_utc: datetime | None,
    *,
    family: str,
) -> AnalysisResult:
    model = definition.metadata
    evidence = AnalysisEvidence(
        model=ModelIdentity(
            model_id=model.model_id,
            model_version=model.model_version,
            implementation_revision=model.implementation_revision,
        ),
        calculation_version=model.calculation_version,
        sources=tuple(
            SourceEvidence(
                identity=source.pin.identity,
                snapshot_sha256=source.pin.snapshot_sha256,
                observed=ObservationRange(
                    start=source.rows[0].session_date,
                    end=source.rows[-1].session_date,
                    count=len(source.rows),
                ),
                aligned_count=len(sessions),
                excluded_count=len(source.rows) - len(sessions),
                quality=source.quality,
            )
            for source in inputs
        ),
        requested=request.window,
        observed_sessions=tuple(sessions),
        parameters=parameters.model_dump(),
        output_fields=model.output_fields,
        quality_policy=request.quality_policy,
        warnings=("quality_override",) if request.quality_policy == "allow_unverified" else (),
    )
    result = AnalysisResult.from_evaluation(
        evidence=evidence,
        points=points,
        evaluated_at_utc=evaluated_at_utc if evaluated_at_utc is not None else datetime.now(UTC),
    )
    if len(result.model_dump_json().encode("utf-8")) > model.execution.max_result_bytes:
        raise AnalyticsError(f"{family} model result exceeds the output size limit")
    definition.validate_result(request, result)
    return result
