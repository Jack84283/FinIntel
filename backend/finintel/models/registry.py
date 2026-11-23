"""Versioned, allowlisted research model registry for Issue #32.

The only executable is a built-in deterministic indicator. Private custom
model modules are deliberately not loaded, imported or discovered.
"""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_validator, model_validator

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

PRICE_INDEX_SOURCES = tuple(
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
)


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
        supported_sources=PRICE_INDEX_SOURCES,
        observations=ObservationRequirements(
            minimum=4, maximum=MAX_MODEL_OBSERVATIONS, parameter_minimum="slow_window + 1"
        ),
        allows_unverified=True,
        output_fields=(
            OutputField(name="session_date", kind="date", units="iso_date", nullable=False),
            OutputField(
                name="close",
                kind="decimal",
                units="index_points",
                nullable=False,
                strictly_positive=True,
            ),
            OutputField(
                name="fast_sma",
                kind="decimal",
                units="index_points",
                nullable=True,
                strictly_positive=True,
            ),
            OutputField(
                name="slow_sma",
                kind="decimal",
                units="index_points",
                nullable=True,
                strictly_positive=True,
            ),
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


class EmaParameters(StrictModel):
    fast_window: int = Field(default=12, ge=1, le=MAX_MODEL_OBSERVATIONS - 1)
    slow_window: int = Field(default=26, ge=2, le=MAX_MODEL_OBSERVATIONS)

    @model_validator(mode="after")
    def validate_order(self) -> EmaParameters:
        if self.fast_window >= self.slow_window:
            raise ValueError("Fast window must be less than slow window")
        return self


class MomentumParameters(StrictModel):
    lookback_sessions: Literal[21, 63, 126, 252] = 63

    @field_validator("lookback_sessions", mode="before")
    @classmethod
    def require_integer_lookback(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Momentum lookback must be an integer")
        return value


class RelativeStrengthParameters(StrictModel):
    """The first pinned source is the benchmark."""


class RsiParameters(StrictModel):
    window: int = Field(default=14, ge=1, le=MAX_MODEL_OBSERVATIONS - 1)
    lower_threshold: int = Field(default=30, ge=0, le=100)
    upper_threshold: int = Field(default=70, ge=0, le=100)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RsiParameters:
        if self.lower_threshold >= self.upper_threshold:
            raise ValueError("Lower RSI threshold must be less than upper threshold")
        return self


def _minimum_ema_observations(parameters: StrictModel) -> int:
    assert isinstance(parameters, EmaParameters)
    return parameters.slow_window


def _minimum_momentum_observations(parameters: StrictModel) -> int:
    assert isinstance(parameters, MomentumParameters)
    return parameters.lookback_sessions + 1


_SESSION = OutputField(name="session_date", kind="date", units="iso_date", nullable=False)
_CLOSE = OutputField(
    name="close", kind="decimal", units="index_points", nullable=False, strictly_positive=True
)
_WARMUP = OutputField(name="warmup_complete", kind="boolean", units="flag", nullable=False)


def _decimal_field(
    name: str, units: str, *, nullable: bool = True, positive: bool = False
) -> OutputField:
    return OutputField(
        name=name, kind="decimal", units=units, nullable=nullable, strictly_positive=positive
    )


def _proposed_price_index_definition(
    *,
    model: str,
    revision: str,
    category: Literal["trend", "momentum", "risk", "regime"],
    description: str,
    parameters: type[StrictModel],
    fields: tuple[OutputField, ...],
    observations: ObservationRequirements,
    parameter_minimum: Callable[[StrictModel], int] | None = None,
    min_sources: int = 1,
    max_sources: int = 1,
    signal_availability: Literal["none", "after_observed_close"] = "none",
    calculation_version: str = CALCULATION_VERSION,
) -> ModelDefinition:
    return ModelDefinition(
        metadata=ModelMetadata(
            model_id=f"builtin.{model}",
            model_version="1.0.0",
            implementation_revision=revision,
            category=category,
            description=description,
            supported_sources=PRICE_INDEX_SOURCES,
            min_sources=min_sources,
            max_sources=max_sources,
            observations=observations,
            allows_unverified=True,
            output_fields=(_SESSION, *fields),
            signal_availability=signal_availability,
            calculation_timing="after_observed_close",
            execution=ExecutionPolicy(
                max_worker_runtime_seconds=MAX_RUNTIME_SECONDS, max_result_bytes=MAX_RESULT_BYTES
            ),
            calculation_version=calculation_version,
            evidence_contract_version="analysis-model-evidence-v1",
        ),
        parameter_type=parameters,
        availability="proposed",
        parameter_minimum=parameter_minimum,
    )


EMA_DEFINITION = _proposed_price_index_definition(
    model="ema_trend",
    revision="ema-sma-seed-decimal-v1",
    category="trend",
    description="SMA-seeded EMA trend and crossover after the observed close",
    parameters=EmaParameters,
    fields=(
        _CLOSE,
        _decimal_field("fast_ema", "index_points", positive=True),
        _decimal_field("slow_ema", "index_points", positive=True),
        _decimal_field("ema_difference", "index_points"),
        OutputField(
            name="trend_after_close",
            kind="enum",
            units="state",
            nullable=True,
            allowed_values=("above", "flat", "below"),
        ),
        OutputField(
            name="crossover_after_close",
            kind="enum",
            units="event",
            nullable=True,
            allowed_values=("above", "none", "below"),
        ),
        _WARMUP,
    ),
    observations=ObservationRequirements(
        minimum=2, maximum=MAX_MODEL_OBSERVATIONS, parameter_minimum="slow_window"
    ),
    parameter_minimum=_minimum_ema_observations,
    signal_availability="after_observed_close",
)

MOMENTUM_DEFINITION = _proposed_price_index_definition(
    model="momentum",
    revision="momentum-trailing-observed-v1",
    category="momentum",
    description="Historical FRED price-index change over observed sessions, excluding dividends",
    parameters=MomentumParameters,
    fields=(
        _CLOSE,
        OutputField(name="comparison_session_date", kind="date", units="iso_date", nullable=True),
        _decimal_field("comparison_close", "index_points", positive=True),
        _decimal_field("momentum", "decimal_return"),
        OutputField(name="lookback_sessions", kind="integer", units="sessions", nullable=False),
        _WARMUP,
    ),
    observations=ObservationRequirements(
        minimum=22, maximum=MAX_MODEL_OBSERVATIONS, parameter_minimum="lookback_sessions + 1"
    ),
    parameter_minimum=_minimum_momentum_observations,
)

RELATIVE_STRENGTH_DEFINITION = _proposed_price_index_definition(
    model="relative_strength",
    revision="relative-strength-common-baseline-v1",
    category="momentum",
    description="Historical price-index performance on common sessions against the first pinned source",
    parameters=RelativeStrengthParameters,
    fields=(
        OutputField(
            name="benchmark",
            kind="enum",
            units="index_code",
            nullable=False,
            allowed_values=tuple(code.lower() for code in SUPPORTED_CODES),
        ),
        *(
            field
            for code in SUPPORTED_CODES
            for field in (
                _decimal_field(f"{code.lower()}_normalized_100", "index_base_100", positive=True),
                _decimal_field(f"{code.lower()}_relative_strength", "ratio", positive=True),
                _decimal_field(f"{code.lower()}_return_difference", "decimal_return_difference"),
            )
        ),
    ),
    observations=ObservationRequirements(minimum=2, maximum=MAX_MODEL_OBSERVATIONS),
    min_sources=2,
    max_sources=4,
)

RSI_DEFINITION = _proposed_price_index_definition(
    model="rsi_state",
    revision="rsi-simple-rolling-states-v1",
    category="momentum",
    description="Descriptive RSI with simple rolling gains and losses",
    parameters=RsiParameters,
    fields=(
        _CLOSE,
        _decimal_field("rsi", "oscillator_0_100"),
        OutputField(
            name="rsi_state",
            kind="enum",
            units="state",
            nullable=True,
            allowed_values=("low", "neutral", "high"),
        ),
        OutputField(
            name="lower_threshold", kind="integer", units="oscillator_0_100", nullable=False
        ),
        OutputField(
            name="upper_threshold", kind="integer", units="oscillator_0_100", nullable=False
        ),
        _WARMUP,
    ),
    observations=ObservationRequirements(minimum=1, maximum=MAX_MODEL_OBSERVATIONS),
)


RISK_MODEL_ID = "builtin.risk_profile"
MIN_TAIL_RETURNS = 60


class RiskParameters(StrictModel):
    risk_window: int = Field(default=63, ge=2, lt=MAX_MODEL_OBSERVATIONS)
    tail_window: int = Field(default=252, ge=MIN_TAIL_RETURNS, lt=MAX_MODEL_OBSERVATIONS)


RISK_DEFINITION = _proposed_price_index_definition(
    model=RISK_MODEL_ID.removeprefix("builtin."),
    revision="historical-risk-linear95-decimal-v1",
    calculation_version="historical-risk-v1",
    category="risk",
    description="Historical price-index risk and optional aligned correlation",
    parameters=RiskParameters,
    fields=(
        _CLOSE,
        *(
            _decimal_field(name, "decimal_return")
            for name in (
                "simple_return",
                "comparison_return",
                "annualized_volatility",
                "downside_deviation",
                "var_95",
                "expected_shortfall_95",
            )
        ),
        _decimal_field("correlation", "ratio"),
        _decimal_field("current_drawdown", "decimal_return", nullable=False),
        _decimal_field("maximum_drawdown", "decimal_return", nullable=False),
        *(
            OutputField(name=name, kind="date", units="iso_date", nullable=True)
            for name in (
                "open_peak_date",
                "open_trough_date",
                "maximum_peak_date",
                "maximum_trough_date",
                "last_recovered_peak_date",
                "last_recovered_trough_date",
                "last_recovery_date",
            )
        ),
        *(
            OutputField(name=name, kind="integer", units="observed_intervals", nullable=True)
            for name in (
                "open_elapsed_intervals",
                "last_peak_to_recovery_intervals",
                "last_trough_to_recovery_intervals",
            )
        ),
        OutputField(
            name="historical_sample_count",
            kind="integer",
            units="observed_intervals",
            nullable=False,
        ),
        OutputField(
            name="historical_tail_count", kind="integer", units="observed_intervals", nullable=True
        ),
        OutputField(
            name="historical_tail_status",
            kind="enum",
            units="risk_status",
            nullable=False,
            allowed_values=("insufficient_history", "empty_tail", "available"),
        ),
    ),
    observations=ObservationRequirements(minimum=1, maximum=MAX_MODEL_OBSERVATIONS),
    max_sources=2,
)


REGIME_MODEL_ID = "builtin.market_regime"


class RegimeParameters(StrictModel):
    short_window: int = Field(default=50, ge=1, lt=MAX_MODEL_OBSERVATIONS)
    long_window: int = Field(default=200, ge=2, le=MAX_MODEL_OBSERVATIONS)
    neutrality_band: str = Field(
        default="0.02",
        pattern=r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$",
        description="Fixed-point fraction from zero through one",
    )
    volatility_window: int = Field(default=63, ge=2, le=MAX_MODEL_OBSERVATIONS - 2)
    calibration_window: int = Field(default=756, ge=1, le=MAX_MODEL_OBSERVATIONS - 3)

    @model_validator(mode="after")
    def validate_windows(self) -> RegimeParameters:
        if self.short_window >= self.long_window:
            raise ValueError("Short SMA window must be less than long SMA window")
        if self.volatility_window + self.calibration_window >= MAX_MODEL_OBSERVATIONS:
            raise ValueError("Volatility and calibration windows exceed the observation cap")
        return self


REGIME_DEFINITION = _proposed_price_index_definition(
    model=REGIME_MODEL_ID.removeprefix("builtin."),
    revision="market-regime-two-axis-v1",
    calculation_version="market-regime-v1",
    category="regime",
    description="Independent historical price trend and realized-volatility classifications",
    parameters=RegimeParameters,
    fields=(
        _CLOSE,
        _decimal_field("sma50", "index_points", positive=True),
        _decimal_field("sma200", "index_points", positive=True),
        _decimal_field("close_distance", "decimal_return"),
        _decimal_field("sma_distance", "decimal_return"),
        OutputField(
            name="trend_state",
            kind="enum",
            units="trend_state",
            nullable=False,
            allowed_values=("uptrend", "downtrend", "sideways", "unknown"),
        ),
        _decimal_field("realized_volatility", "decimal_return"),
        _decimal_field("volatility_p25", "decimal_return"),
        _decimal_field("volatility_p75", "decimal_return"),
        OutputField(
            name="volatility_calibration_count",
            kind="integer",
            units="observations",
            nullable=False,
        ),
        OutputField(
            name="volatility_state",
            kind="enum",
            units="volatility_state",
            nullable=False,
            allowed_values=("low", "normal", "high", "unknown"),
        ),
        OutputField(
            name="reason_codes",
            kind="reason_codes",
            units="classification_reasons",
            nullable=False,
            allowed_values=(
                "trend_above_band",
                "trend_below_band",
                "trend_mixed",
                "trend_insufficient_history",
                "vol_below_p25",
                "vol_above_p75",
                "vol_normal_range",
                "vol_calibration_insufficient",
                "quality_unverified",
            ),
        ),
    ),
    observations=ObservationRequirements(minimum=1, maximum=MAX_MODEL_OBSERVATIONS),
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


_DEFINITIONS = _index_definitions(
    SMA_DEFINITION,
    EMA_DEFINITION,
    MOMENTUM_DEFINITION,
    RELATIVE_STRENGTH_DEFINITION,
    RSI_DEFINITION,
    RISK_DEFINITION,
    REGIME_DEFINITION,
)


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
