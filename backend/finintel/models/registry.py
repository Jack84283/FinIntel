"""Versioned, allowlisted research model registry for Issue #32.

The only executable is a built-in deterministic indicator. Private custom
model modules are deliberately not loaded, imported or discovered.
"""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from finintel.market_data.analytics import CALCULATION_VERSION
from finintel.models.contracts import (
    AnalysisRequest,
    AnalysisResult,
    ExecutionPolicy,
    ModelKey,
    ModelMetadata,
    ObservationRequirements,
    OutputField,
    SourceIdentity,
    StrictModel,
    canonical_sha256,
)

MODEL_ID = "builtin.sma_crossover"
MODEL_VERSION = "1.0.0"
# Bump when model calculations or signal semantics change.
IMPLEMENTATION_REVISION = "sma-crossover-observed-close-v1"
MAX_MODEL_OBSERVATIONS = 2000
MAX_RUNTIME_SECONDS = 90
MAX_RESULT_BYTES = 4 * 1024 * 1024

SUPPORTED_CODES = ("SP500", "DJIA", "NASDAQCOM", "NDX")


class ModelParameters(StrictModel):
    fast_window: int = Field(default=5, ge=2, le=100)
    slow_window: int = Field(default=20, ge=3, le=252)

    @model_validator(mode="after")
    def validate_order(self) -> ModelParameters:
        if self.fast_window >= self.slow_window:
            raise ValueError("Fast window must be less than slow window")
        return self


def model_registry() -> dict:
    """Return public built-in metadata, never arbitrary private model paths."""
    return {
        "registry_version": "models-registry-v1",
        "models": [
            {
                "model_id": MODEL_ID,
                "model_version": MODEL_VERSION,
                "implementation_revision": IMPLEMENTATION_REVISION,
                "description": "Trailing SMA crossover signal (research only)",
                "parameter_schema": ModelParameters.model_json_schema(),
                "input_contract": {
                    "instrument_kind": "index",
                    "provider": "fred",
                    "allowed_codes": list(SUPPORTED_CODES),
                    "adjustment": "price_index",
                    "return_semantics": "price_index",
                    "frequency": "daily",
                    "units": "index_points",
                    "minimum_observations": "slow_window + 1",
                    "maximum_observations": MAX_MODEL_OBSERVATIONS,
                    "point_in_time_membership": "not_used",
                },
                "output_schema": {
                    "session_date": "ISO date",
                    "close": "positive Decimal string",
                    "fast_sma": "Decimal string or null",
                    "slow_sma": "Decimal string or null",
                    "signal_after_close": "long, flat or null",
                },
                "execution": {
                    "builtin_only": True,
                    "read_only": True,
                    "no_network": True,
                    "no_dynamic_code": True,
                    "no_orders": True,
                    "no_backtest": True,
                    "no_synthetic_sessions": True,
                    "signal_timing": "computed after observed close; not tradable at same close",
                },
            }
        ],
    }


class ModelDefinition(StrictModel):
    """Static catalog metadata and an owned Pydantic parameter contract."""

    metadata: ModelMetadata
    parameter_type: type[StrictModel]
    availability: Literal["released", "proposed"]
    parameter_minimum: Callable[[StrictModel], int] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_definition(self) -> ModelDefinition:
        if bool(self.parameter_minimum) != bool(self.metadata.observations.parameter_minimum):
            raise ValueError("Parameterized bounds require both metadata and a typed rule")
        return self

    def validate_request(self, request: AnalysisRequest) -> StrictModel:
        """Check the request against discovery metadata without granting execution."""
        metadata = self.metadata
        if (request.model_id, request.model_version) != (
            metadata.model_id,
            metadata.model_version,
        ):
            raise ValueError("Request model ID or version does not match this definition")
        identities = tuple(source.identity for source in request.sources)
        if not metadata.min_sources <= len(identities) <= metadata.max_sources:
            raise ValueError("Invalid number of sources for this model")
        if any(source not in metadata.supported_sources for source in identities):
            raise ValueError("Unsupported source identity for this model")
        if request.quality_policy == "allow_unverified" and not metadata.allows_unverified:
            raise ValueError("This model requires complete source quality")
        return self.parameter_type.model_validate(request.parameters)

    def validate_result(self, request: AnalysisRequest, result: AnalysisResult) -> None:
        """Bind results to their exact model definition and pinned request before storage."""
        parameters = self.validate_request(request)
        # Frozen envelopes still contain parameter and point dictionaries. Reparse
        # them at the consumption boundary to catch mutation or unvalidated copies.
        result = AnalysisResult.model_validate_json(result.model_dump_json())
        evidence = result.evidence
        metadata = self.metadata
        if (
            evidence.model.model_id != metadata.model_id
            or evidence.model.model_version != metadata.model_version
            or evidence.model.implementation_revision != metadata.implementation_revision
            or evidence.calculation_version != metadata.calculation_version
            or evidence.contract_version != metadata.evidence_contract_version
            or evidence.output_fields != metadata.output_fields
        ):
            raise ValueError("Result does not match the registered model evidence contract")
        if (
            evidence.requested != request.window
            or evidence.quality_policy != request.quality_policy
            or canonical_sha256(evidence.parameters) != canonical_sha256(parameters.model_dump())
            or tuple((source.identity, source.snapshot_sha256) for source in evidence.sources)
            != tuple((source.identity, source.snapshot_sha256) for source in request.sources)
        ):
            raise ValueError("Result evidence does not match the pinned analysis request")
        minimum = (
            self.parameter_minimum(parameters)
            if self.parameter_minimum is not None
            else metadata.observations.minimum
        )
        if any(
            not minimum <= source.observed.count <= metadata.observations.maximum
            for source in evidence.sources
        ):
            raise ValueError("Result source observation count is outside the model contract")


def _minimum_sma_observations(parameters: StrictModel) -> int:
    assert isinstance(parameters, ModelParameters)
    return parameters.slow_window + 1


SMA_DEFINITION = ModelDefinition(
    metadata=ModelMetadata(
        model_id=MODEL_ID,
        model_version=MODEL_VERSION,
        implementation_revision=IMPLEMENTATION_REVISION,
        category="trend",
        description="Trailing SMA crossover signal (research only)",
        supported_sources=tuple(
            SourceIdentity(
                instrument_kind="index",
                code=code,
                provider="fred",
                adjustment="price_index",
                return_semantics="price_index",
                frequency="daily",
                units="index_points",
            )
            for code in SUPPORTED_CODES
        ),
        observations=ObservationRequirements(
            minimum=4, maximum=MAX_MODEL_OBSERVATIONS, parameter_minimum="slow_window + 1"
        ),
        allows_unverified=True,
        output_fields=(
            OutputField(name="session_date", kind="date", units="iso_date", nullable=False),
            OutputField(name="close", kind="decimal", units="index_points", nullable=False),
            OutputField(name="fast_sma", kind="decimal", units="index_points", nullable=True),
            OutputField(name="slow_sma", kind="decimal", units="index_points", nullable=True),
            OutputField(
                name="signal_after_close",
                kind="enum",
                units="signal",
                nullable=True,
                allowed_values=("long", "flat"),
            ),
        ),
        signal_availability="after_observed_close",
        calculation_timing="after_observed_close",
        execution=ExecutionPolicy(
            max_worker_runtime_seconds=MAX_RUNTIME_SECONDS, max_result_bytes=MAX_RESULT_BYTES
        ),
        calculation_version=CALCULATION_VERSION,
        evidence_contract_version="analysis-model-evidence-v1",
    ),
    parameter_type=ModelParameters,
    availability="released",
    parameter_minimum=_minimum_sma_observations,
)


# Catalog registration is never an executable dispatch table. Legacy API, worker,
# and engine still independently allowlist only the original built-in SMA.
def _index_definitions(
    *definitions: ModelDefinition,
) -> MappingProxyType[tuple[str, str], ModelDefinition]:
    indexed = {}
    for definition in definitions:
        key = (definition.metadata.model_id, definition.metadata.model_version)
        if key in indexed:
            raise ValueError("Duplicate analysis model ID and version")
        indexed[key] = definition
    return MappingProxyType(indexed)


_DEFINITIONS = _index_definitions(SMA_DEFINITION)


def get_model_definition(key: ModelKey) -> ModelDefinition:
    try:
        return _DEFINITIONS[(key.model_id, key.model_version)]
    except KeyError:
        raise ValueError("Unknown analysis model ID or version") from None


def analysis_model_registry() -> dict:
    """Publish bounded v2 metadata without exposing code or execution bindings."""
    return {
        "registry_version": "models-registry-v2",
        "models": [
            {
                **definition.metadata.model_dump(mode="json"),
                "availability": definition.availability,
                "parameter_schema": definition.parameter_type.model_json_schema(),
            }
            for definition in _DEFINITIONS.values()
        ],
    }
