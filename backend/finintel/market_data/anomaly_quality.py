"""Configurable, conservative anomaly candidates for local quality scans.

The rules in this module describe observations that deserve review. They do
not assert a bad provider response, a corporate action, or a correction, and
they never mutate the source observation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

ANOMALY_RULE_VERSION = "anomaly-detection-v1"


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class AnomalyThresholds:
    """Documented defaults shared by the scanner and its API callers."""

    price_jump_ratio: Decimal = Decimal(2)
    volume_spike_ratio: Decimal = Decimal(10)
    volume_spike_minimum: int = 10_000
    volume_baseline_minimum: int = 100
    zero_volume_price_move_ratio: Decimal = Decimal("0.10")
    zero_volume_baseline_minimum: int = 100
    stale_after_days: int = 30
    volume_lookback: int = 20
    reconciliation_relative_tolerance: Decimal = Decimal("0.01")
    reconciliation_absolute_tolerance: Decimal = Decimal(0)
    reconciliation_provider_priority: tuple[str, ...] = (
        "alpaca_sip", "tiingo", "alpha_vantage"
    )

    def __post_init__(self) -> None:
        if self.price_jump_ratio <= 1:
            raise ValueError("price_jump_ratio must be greater than 1")
        if self.volume_spike_ratio <= 1:
            raise ValueError("volume_spike_ratio must be greater than 1")
        if self.volume_spike_minimum < 1:
            raise ValueError("volume_spike_minimum must be positive")
        if self.volume_baseline_minimum < 1:
            raise ValueError("volume_baseline_minimum must be positive")
        if not 0 < self.zero_volume_price_move_ratio <= 1:
            raise ValueError("zero_volume_price_move_ratio must be between 0 and 1")
        if self.zero_volume_baseline_minimum < 1:
            raise ValueError("zero_volume_baseline_minimum must be positive")
        if self.stale_after_days < 1:
            raise ValueError("stale_after_days must be positive")
        if self.volume_lookback < 2:
            raise ValueError("volume_lookback must be at least 2")
        if (
            not self.reconciliation_relative_tolerance.is_finite()
            or not 0 <= self.reconciliation_relative_tolerance <= 1
        ):
            raise ValueError("reconciliation_relative_tolerance must be between 0 and 1")
        if (
            not self.reconciliation_absolute_tolerance.is_finite()
            or self.reconciliation_absolute_tolerance < 0
        ):
            raise ValueError("reconciliation_absolute_tolerance must be nonnegative")
        if set(self.reconciliation_provider_priority) != {
            "alpaca_sip", "tiingo", "alpha_vantage"
        } or len(self.reconciliation_provider_priority) != 3:
            raise ValueError("reconciliation_provider_priority must list the three providers once")


def normalize_thresholds(
    value: AnomalyThresholds | Mapping[str, object] | None,
) -> AnomalyThresholds:
    """Validate caller-supplied thresholds without silently accepting typos."""

    if value is None:
        return AnomalyThresholds()
    if isinstance(value, AnomalyThresholds):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("thresholds must be a mapping or AnomalyThresholds")
    fields = {
        "price_jump_ratio",
        "volume_spike_ratio",
        "volume_spike_minimum",
        "volume_baseline_minimum",
        "zero_volume_price_move_ratio",
        "zero_volume_baseline_minimum",
        "stale_after_days",
        "volume_lookback",
        "reconciliation_relative_tolerance",
        "reconciliation_absolute_tolerance",
        "reconciliation_provider_priority",
    }
    unknown = set(value) - fields
    if unknown:
        raise ValueError(f"Unknown anomaly threshold(s): {', '.join(sorted(map(str, unknown)))}")
    defaults = AnomalyThresholds()
    decimal_fields = {
        "price_jump_ratio",
        "volume_spike_ratio",
        "zero_volume_price_move_ratio",
        "reconciliation_relative_tolerance",
        "reconciliation_absolute_tolerance",
    }
    integer_fields = fields - decimal_fields - {"reconciliation_provider_priority"}
    converted: dict[str, object] = {}
    for field in fields:
        if field not in value:
            continue
        raw = value[field]
        if field in decimal_fields:
            parsed = _decimal(raw)
            if parsed is None:
                raise ValueError(f"{field} must be a finite decimal")
            converted[field] = parsed
        elif field == "reconciliation_provider_priority":
            if not isinstance(raw, (list, tuple)) or any(not isinstance(p, str) for p in raw):
                raise ValueError("reconciliation_provider_priority must be a provider list")
            converted[field] = tuple(raw)
        elif field in integer_fields:
            if isinstance(raw, bool):
                raise ValueError(f"{field} must be an integer")
            try:
                converted[field] = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be an integer") from exc
    return AnomalyThresholds(**{**defaults.__dict__, **converted})


def thresholds_identity(
    thresholds: AnomalyThresholds | Mapping[str, object] | None,
) -> dict[str, object]:
    """Return a stable, JSON-safe policy identity for scan checkpoints."""

    normalized = normalize_thresholds(thresholds)
    return {
        "rule_version": ANOMALY_RULE_VERSION,
        "price_jump_ratio": str(normalized.price_jump_ratio),
        "volume_spike_ratio": str(normalized.volume_spike_ratio),
        "volume_spike_minimum": normalized.volume_spike_minimum,
        "volume_baseline_minimum": normalized.volume_baseline_minimum,
        "zero_volume_price_move_ratio": str(normalized.zero_volume_price_move_ratio),
        "zero_volume_baseline_minimum": normalized.zero_volume_baseline_minimum,
        "stale_after_days": normalized.stale_after_days,
        "volume_lookback": normalized.volume_lookback,
        "reconciliation_relative_tolerance": str(normalized.reconciliation_relative_tolerance),
        "reconciliation_absolute_tolerance": str(normalized.reconciliation_absolute_tolerance),
        "reconciliation_provider_priority": list(normalized.reconciliation_provider_priority),
    }


def parse_timestamp(value: object) -> datetime | None:
    """Parse stored provider timestamps as UTC without guessing invalid values."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def invalid_ohlcv_fields(values: Mapping[str, object], volume: object) -> list[str]:
    """Return impossible fields while tolerating SQLite's TEXT decimals."""

    parsed = {name: _decimal(values.get(name)) for name in ("open", "high", "low", "close")}
    invalid = [name for name, value in parsed.items() if value is None or value <= 0]
    if not invalid:
        if parsed["high"] < parsed["low"]:
            invalid.append("high_low_order")
        if not parsed["low"] <= parsed["open"] <= parsed["high"]:
            invalid.append("open_order")
        if not parsed["low"] <= parsed["close"] <= parsed["high"]:
            invalid.append("close_order")
    try:
        volume_value = int(volume)
    except (TypeError, ValueError):
        volume_value = None
    if volume_value is None or volume_value < 0:
        invalid.append("volume")
    return invalid


def price_jump_candidate(
    previous_close: object,
    close: object,
    *,
    adjustment: str,
    thresholds: AnomalyThresholds | Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Describe a large close change, preserving raw/adjusted semantics."""

    normalized = normalize_thresholds(thresholds)
    previous = _decimal(previous_close)
    current = _decimal(close)
    if previous is None or current is None or previous <= 0 or current <= 0:
        return None
    ratio = current / previous if current >= previous else previous / current
    if ratio < normalized.price_jump_ratio:
        return None
    raw_unadjusted = adjustment == "raw_unadjusted"
    return {
        "rule": "price_jump_candidate",
        "severity": "warning",
        "confidence": "low",
        "evidence": {
            "anomaly_rule_version": ANOMALY_RULE_VERSION,
            "candidate_only": True,
            "threshold": str(normalized.price_jump_ratio),
            "previous_close": str(previous),
            "close": str(current),
            "ratio": str(ratio),
            "adjustment": adjustment,
            "series_semantics": "raw_unadjusted" if raw_unadjusted else adjustment,
            "possible_corporate_action": raw_unadjusted,
            "corporate_action_status": "not_checked",
        },
    }


def volume_spike_candidate(
    current_volume: object,
    history: Sequence[object],
    *,
    thresholds: AnomalyThresholds | Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Flag a large volume increase only when the baseline is liquid enough."""

    normalized = normalize_thresholds(thresholds)
    try:
        current = int(current_volume)
    except (TypeError, ValueError):
        return None
    parsed_history = [_decimal(item) for item in history]
    volumes = [item for item in parsed_history if item is not None and item >= 0]
    volumes = volumes[-normalized.volume_lookback :]
    if not volumes:
        return None
    baseline = sorted(volumes)[len(volumes) // 2]
    if baseline < normalized.volume_baseline_minimum:
        return None
    ratio = Decimal(current) / baseline if baseline > 0 else None
    if (
        ratio is None
        or ratio < normalized.volume_spike_ratio
        or current < normalized.volume_spike_minimum
    ):
        return None
    return {
        "rule": "volume_spike_candidate",
        "severity": "warning",
        "confidence": "low",
        "evidence": {
            "anomaly_rule_version": ANOMALY_RULE_VERSION,
            "candidate_only": True,
            "threshold": str(normalized.volume_spike_ratio),
            "baseline_volume": str(baseline),
            "volume": current,
            "ratio": str(ratio),
            "lookback_observations": len(volumes),
            "illiquid_baseline_excluded": False,
        },
    }


def zero_volume_candidate(
    previous_close: object,
    close: object,
    current_volume: object,
    history: Sequence[object],
    *,
    thresholds: AnomalyThresholds | Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Flag zero volume only when a meaningful liquid-session move accompanies it."""

    normalized = normalize_thresholds(thresholds)
    try:
        if int(current_volume) != 0:
            return None
    except (TypeError, ValueError):
        return None
    previous = _decimal(previous_close)
    current = _decimal(close)
    volumes = [_decimal(item) for item in history]
    baseline = max((item for item in volumes if item is not None and item >= 0), default=Decimal(0))
    if (
        previous is None
        or current is None
        or previous <= 0
        or baseline < normalized.zero_volume_baseline_minimum
    ):
        return None
    move = abs(current - previous) / previous
    if move < normalized.zero_volume_price_move_ratio:
        return None
    return {
        "rule": "suspicious_zero_volume",
        "severity": "warning",
        "confidence": "low",
        "evidence": {
            "anomaly_rule_version": ANOMALY_RULE_VERSION,
            "candidate_only": True,
            "price_move": str(move),
            "threshold": str(normalized.zero_volume_price_move_ratio),
            "previous_close": str(previous),
            "close": str(current),
            "baseline_volume": str(baseline),
            "volume": 0,
        },
    }
