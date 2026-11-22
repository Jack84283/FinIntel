"""Versioned Analysis Library discovery and evidence contracts.

The original SMA, durable job, and backtest wire formats remain separate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from re import fullmatch
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

ModelId = Annotated[str, Field(pattern=r"^builtin\.[a-z][a-z0-9_]*$")]
ModelVersion = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
Revision = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
QualityPolicy = Literal["require_complete", "allow_unverified"]
Scalar = str | int | bool | None
ReasonCodes = tuple[Revision, ...]
OutputValue = Scalar | ReasonCodes


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Expected a Boolean")
    return value


TrueFlag = Annotated[Literal[True], BeforeValidator(_strict_bool)]
FalseFlag = Annotated[Literal[False], BeforeValidator(_strict_bool)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelKey(StrictModel):
    model_id: ModelId
    model_version: ModelVersion


class ModelIdentity(ModelKey):
    implementation_revision: Revision


class SourceIdentity(StrictModel):
    instrument_kind: Literal["index"]
    code: Literal["SP500", "DJIA", "NASDAQCOM", "NDX"]
    provider: Literal["fred"]
    adjustment: Literal["price_index"]
    return_semantics: Literal["price_index"]
    frequency: Literal["daily"]
    units: Literal["index_points"]


class SourcePin(StrictModel):
    identity: SourceIdentity
    snapshot_sha256: Sha256


class DateRange(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> DateRange:
        if self.start > self.end:
            raise ValueError("Start date must precede or equal end date")
        return self


class ObservationRange(DateRange):
    count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_count(self) -> ObservationRange:
        span = (self.end - self.start).days + 1
        if self.count > span or (self.count == 1 and span != 1):
            raise ValueError("Observation count does not fit the observed window")
        return self


class QualityEvidence(StrictModel):
    status: Literal["not_run", "partial", "stale", "blocked", "failed", "verified"]
    warnings: tuple[Revision, ...] = ()

    @model_validator(mode="after")
    def validate_warning(self) -> QualityEvidence:
        if self.status != "verified" and not self.warnings:
            raise ValueError("Unverified quality needs an explicit warning")
        return self


class SourceEvidence(SourcePin):
    observed: ObservationRange
    aligned_count: Annotated[int, Field(ge=1)]
    excluded_count: Annotated[int, Field(ge=0)]
    quality: QualityEvidence

    @model_validator(mode="after")
    def validate_alignment_counts(self) -> SourceEvidence:
        if self.aligned_count + self.excluded_count != self.observed.count:
            raise ValueError("Aligned and excluded counts must sum to source observations")
        return self


class OutputField(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    kind: Literal["date", "decimal", "integer", "boolean", "enum", "reason_codes"]
    units: Revision
    nullable: bool
    strictly_positive: bool = False
    allowed_values: tuple[Revision, ...] = ()

    def accepts(self, value: OutputValue) -> bool:
        if value is None:
            return self.nullable
        match self.kind:
            case "date":
                if not isinstance(value, str):
                    return False
                try:
                    return date.fromisoformat(value).isoformat() == value
                except ValueError:
                    return False
            case "decimal":
                if not isinstance(value, str) or not fullmatch(
                    r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value
                ):
                    return False
                return not self.strictly_positive or Decimal(value) > 0
            case "integer":
                return type(value) is int
            case "boolean":
                return type(value) is bool
            case "enum":
                return isinstance(value, str) and value in self.allowed_values
            case "reason_codes":
                return (
                    isinstance(value, tuple)
                    and len(set(value)) == len(value)
                    and all(code in self.allowed_values for code in value)
                )

    @model_validator(mode="after")
    def validate_constraints(self) -> OutputField:
        if self.strictly_positive and self.kind != "decimal":
            raise ValueError("Strict positivity is only supported for decimal output fields")
        if (self.kind in {"enum", "reason_codes"}) != bool(self.allowed_values):
            raise ValueError("Enum and reason-code fields require declared allowed values")
        if len(set(self.allowed_values)) != len(self.allowed_values):
            raise ValueError("Duplicate output allowed values")
        return self


class ObservationRequirements(StrictModel):
    minimum: Annotated[int, Field(ge=1)]
    maximum: Annotated[int, Field(ge=1)]
    parameter_minimum: str | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> ObservationRequirements:
        if self.minimum > self.maximum:
            raise ValueError("Minimum exceeds maximum observations")
        return self


class ExecutionPolicy(StrictModel):
    read_only: TrueFlag = True
    network_access: FalseFlag = False
    dynamic_code: FalseFlag = False
    order_execution: FalseFlag = False
    synthetic_sessions: FalseFlag = False
    max_worker_runtime_seconds: Annotated[int, Field(gt=0)]
    max_result_bytes: Annotated[int, Field(gt=0)]


class ModelMetadata(ModelIdentity):
    category: Literal["trend", "momentum", "risk", "regime"]
    description: Annotated[str, Field(min_length=1)]
    research_only: TrueFlag = True
    supported_sources: tuple[SourceIdentity, ...] = Field(min_length=1)
    min_sources: Annotated[int, Field(ge=1)] = 1
    max_sources: Annotated[int, Field(ge=1)] = 1
    observations: ObservationRequirements
    default_quality_policy: Literal["require_complete"] = "require_complete"
    allows_unverified: bool
    output_fields: tuple[OutputField, ...] = Field(min_length=1)
    signal_availability: Literal["none", "after_observed_close"]
    calculation_timing: Literal["after_observed_close"]
    execution: ExecutionPolicy
    calculation_version: Revision
    evidence_contract_version: Revision

    @model_validator(mode="after")
    def validate_metadata(self) -> ModelMetadata:
        if (
            self.min_sources > self.max_sources
            or self.max_sources > len(set(self.supported_sources))
            or len(set(self.supported_sources)) != len(self.supported_sources)
        ):
            raise ValueError("Invalid model source limits or duplicate identities")
        if len({field.name for field in self.output_fields}) != len(self.output_fields):
            raise ValueError("Duplicate output field")
        return self


class AnalysisRequest(ModelKey):
    contract_version: Literal["analysis-model-request-v1"] = "analysis-model-request-v1"
    sources: tuple[SourcePin, ...] = Field(min_length=1)
    window: DateRange
    parameters: dict[str, Scalar] = Field(default_factory=dict)
    quality_policy: QualityPolicy = "require_complete"

    @model_validator(mode="after")
    def validate_request(self) -> AnalysisRequest:
        if self.window.end > datetime.now(UTC).date():
            raise ValueError("Analysis window cannot end in the future")
        if len({source.identity for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate analysis source identity")
        return self


class AnalysisEvidence(StrictModel):
    contract_version: Literal["analysis-model-evidence-v1"] = "analysis-model-evidence-v1"
    model: ModelIdentity
    calculation_version: Revision
    sources: tuple[SourceEvidence, ...] = Field(min_length=1)
    requested: DateRange
    observed_sessions: tuple[date, ...] = Field(min_length=1)
    parameters: dict[str, Scalar]
    output_fields: tuple[OutputField, ...] = Field(min_length=1)
    quality_policy: QualityPolicy
    warnings: tuple[Revision, ...] = ()
    research_only: TrueFlag = True

    @model_validator(mode="after")
    def validate_evidence(self) -> AnalysisEvidence:
        if len({source.identity for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate analysis source evidence")
        aligned_sessions = set(self.observed_sessions)
        if tuple(sorted(aligned_sessions)) != self.observed_sessions:
            raise ValueError("Observed sessions must be unique and increasing")
        for source in self.sources:
            if (
                not self.requested.start
                <= source.observed.start
                <= source.observed.end
                <= self.requested.end
            ):
                raise ValueError("Source observations must lie inside the requested window")
            if (
                not source.observed.start
                <= self.observed_sessions[0]
                <= self.observed_sessions[-1]
                <= source.observed.end
            ):
                raise ValueError("Result observations must lie inside each source window")
            if source.aligned_count != len(aligned_sessions):
                raise ValueError("Source alignment count must match result sessions")
            known_sessions = aligned_sessions | {source.observed.start, source.observed.end}
            if len(known_sessions) > source.observed.count:
                raise ValueError(
                    "Source count cannot cover both observed endpoints and aligned sessions"
                )
            if self.quality_policy == "require_complete" and source.quality.status != "verified":
                raise ValueError("Complete quality policy requires verified source evidence")
        if self.quality_policy == "allow_unverified" and "quality_override" not in self.warnings:
            raise ValueError("Explicit quality override requires a result warning")
        return self


def canonical_sha256(payload: object) -> str:
    """Hash JSON-compatible evidence using the Analysis Library v1 encoding."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AnalysisResult(StrictModel):
    contract_version: Literal["analysis-model-result-v1"] = "analysis-model-result-v1"
    evidence: AnalysisEvidence
    points: tuple[dict[str, OutputValue], ...]
    reproducibility_sha256: Sha256
    output_sha256: Sha256
    evaluated_at_utc: datetime

    @model_validator(mode="after")
    def validate_hashes(self) -> AnalysisResult:
        if self.evaluated_at_utc.utcoffset() != timedelta(0):
            raise ValueError("Evaluation timestamp must be in UTC")
        fields = {field.name: field for field in self.evidence.output_fields}
        if len(self.points) != len(self.evidence.observed_sessions):
            raise ValueError("Result point count must match observed sessions")
        for point, session in zip(self.points, self.evidence.observed_sessions, strict=True):
            if point.keys() != fields.keys() or any(
                not field.accepts(point[name]) for name, field in fields.items()
            ):
                raise ValueError("Point does not match the declared output schema")
            if "session_date" in fields and point["session_date"] != session.isoformat():
                raise ValueError("Point date does not match result observation date")
        if self.reproducibility_sha256 != canonical_sha256(self.evidence.model_dump(mode="json")):
            raise ValueError("Analysis evidence hash mismatch")
        if self.output_sha256 != canonical_sha256(self.points):
            raise ValueError("Analysis output hash mismatch")
        return self

    @classmethod
    def from_evaluation(
        cls,
        *,
        evidence: AnalysisEvidence,
        points: tuple[dict[str, OutputValue], ...],
        evaluated_at_utc: datetime,
    ) -> AnalysisResult:
        """Build and validate result hashes from deterministic evidence and output."""
        return cls(
            evidence=evidence,
            points=points,
            reproducibility_sha256=canonical_sha256(evidence.model_dump(mode="json")),
            output_sha256=canonical_sha256(points),
            evaluated_at_utc=evaluated_at_utc,
        )
