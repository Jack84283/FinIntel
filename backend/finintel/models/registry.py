"""Versioned, allowlisted research model registry for Issue #32.

The only executable is a built-in deterministic indicator. Private custom
model modules are deliberately not loaded, imported or discovered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_ID = "builtin.sma_crossover"
MODEL_VERSION = "1.0.0"
# Bump when model calculations or signal semantics change.
IMPLEMENTATION_REVISION = "sma-crossover-observed-close-v1"
MAX_MODEL_OBSERVATIONS = 2000


class ModelParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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
                    "allowed_codes": ["SP500", "DJIA", "NASDAQCOM", "NDX"],
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
