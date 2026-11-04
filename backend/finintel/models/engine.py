"""Reproducible indicator execution on immutable local-source identities.

This is a bounded *synchronous research evaluation*, not the durable model-job
runner or an executable/financial backtest. All data come from analytics_store's
read-only SQLite snapshot and explicit FRED index identity. Never run user code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_store import run_analytics
from finintel.models.registry import (
    IMPLEMENTATION_REVISION,
    MAX_MODEL_OBSERVATIONS,
    MODEL_ID,
    MODEL_VERSION,
    ModelParameters,
)


def _sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sma(levels: list[Decimal], end: int, window: int) -> str:
    with localcontext() as context:
        context.prec = 28
        average = sum(levels[end + 1 - window : end + 1], Decimal(0)) / Decimal(window)
        return format(+average, "f")


def evaluate_builtin_indicator(
    *,
    model_id: str = MODEL_ID,
    model_version: str = MODEL_VERSION,
    code: str = "SP500",
    start: date,
    end: date,
    parameters: ModelParameters | None = None,
    quality_policy: str = "require_complete",
    expected_snapshot_sha256: str | None = None,
    db: Path | None = None,
) -> dict:
    """Return actual dated closes and causal SMA decisions, with stable hashes.

    A signal from close(t) can only be acted on *after* close(t). The feature
    window contains t and earlier actual observations; no prior/next session is
    fabricated and no ex-post backtest performance is claimed.
    """
    if model_id != MODEL_ID or model_version != MODEL_VERSION:
        raise AnalyticsError("Unsupported model ID or version; no dynamic model execution")
    if code not in {"SP500", "DJIA", "NASDAQCOM", "NDX"}:
        raise AnalyticsError("Only supported FRED price indexes; volatility returns excluded")
    if start > end or end > datetime.now(UTC).date():
        raise AnalyticsError("Invalid or future model evaluation date range")
    if quality_policy not in {"require_complete", "allow_unverified"}:
        raise AnalyticsError("Unsupported model data-quality policy")
    params = parameters if parameters is not None else ModelParameters()
    if not isinstance(params, ModelParameters):
        raise AnalyticsError("Invalid typed model parameters")
    if expected_snapshot_sha256 is not None and (
        len(expected_snapshot_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_snapshot_sha256)
    ):
        raise AnalyticsError("Invalid expected source snapshot hash")

    source = run_analytics(
        kind="index",
        code=code,
        provider="fred",
        start=start,
        end=end,
        window=20,
        quality_policy=quality_policy,
        db=db,
    )
    identity = source["identity"]
    if (
        identity["instrument_kind"] != "index"
        or identity["provider"] != "fred"
        or identity["adjustment"] != "price_index"
        or identity["return_semantics"] != "price_index"
        or identity["frequency"] != "daily"
        or identity["units"] != "index_points"
    ):
        raise AnalyticsError("Incompatible model source adjustment or return semantics")
    snapshot = source["source"]["snapshot_sha256"]
    if expected_snapshot_sha256 is not None and expected_snapshot_sha256 != snapshot:
        raise AnalyticsError("Local source snapshot changed; refresh model inputs")

    rows = source["points"]
    if len(rows) > MAX_MODEL_OBSERVATIONS:
        raise AnalyticsError("Model limit exceeds 2,000 observations; narrow the date range")
    if len(rows) < params.slow_window + 1:
        raise AnalyticsError("Insufficient observed sessions for slow window plus signal evaluation")

    levels = [Decimal(row["close"]) for row in rows]
    output = []
    for index, row in enumerate(rows):
        fast = _sma(levels, index, params.fast_window) if index + 1 >= params.fast_window else None
        slow = _sma(levels, index, params.slow_window) if index + 1 >= params.slow_window else None
        signal = (
            "long" if Decimal(fast) > Decimal(slow) else "flat"
        ) if fast is not None and slow is not None else None
        output.append(
            {
                "session_date": row["session_date"],
                "close": row["close"],
                "fast_sma": fast,
                "slow_sma": slow,
                "signal_after_close": signal,
            }
        )

    lineage = {
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "parameters": params.model_dump(),
        "source_identity": identity,
        "source_snapshot_sha256": snapshot,
        "analysis_calculation_version": source["calculation_version"],
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "first_observed_session": source["first_session"],
        "last_observed_session": source["last_session"],
        "quality_policy": quality_policy,
        "quality_status": source["quality"]["status"],
        "quality_verified": source["quality"]["quality_verified"],
        "point_in_time_membership": "not_used",
        "dividends_included": False,
        "corporate_action_adjustment": "none_index_price_level",
    }
    return {
        "execution_contract": "model-research-evaluation-v1",
        "lineage": lineage,
        "reproducibility_sha256": _sha256(lineage),
        "output_sha256": _sha256(output),
        "evaluated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "completed",
        "observations": len(rows),
        "points": output,
        "quality_warning": (
            None if source["quality"]["quality_verified"] else
            "Unverified data: research-only results, not verified for backtesting"
        ),
        "read_only": True,
        "no_network": True,
        "no_synthetic_sessions": True,
        "signal_after_close_only": True,
        "backtest_validated": False,
        "persistent_job": False,
    }
