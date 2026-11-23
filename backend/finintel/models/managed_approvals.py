"""Exact analysis implementation revisions approved for isolated execution."""

from types import MappingProxyType

MANAGED_ANALYSIS_REVISIONS = MappingProxyType(
    {
        ("builtin.ema_trend", "1.0.0"): "ema-sma-seed-decimal-v1",
        ("builtin.momentum", "1.0.0"): "momentum-trailing-observed-v1",
        ("builtin.relative_strength", "1.0.0"): "relative-strength-common-baseline-v1",
        ("builtin.rsi_state", "1.0.0"): "rsi-simple-rolling-states-v1",
        ("builtin.risk_profile", "1.0.0"): "historical-risk-linear95-decimal-v1",
        ("builtin.market_regime", "1.0.0"): "market-regime-two-axis-v1",
    }
)
