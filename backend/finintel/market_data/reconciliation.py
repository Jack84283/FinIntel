"""Conservative, provider-independent reconciliation of local US daily equity bars.

This module only produces quality *candidates*. It does not promote a preferred
source into a canonical price, update stored observations, or infer corporate actions.
The eligible US equity adapters explicitly supply raw USD/share daily closes.
Other instruments or adjustment/return conventions require an explicit contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

from finintel.storage.db import _read_connection

RECONCILIATION_RULE_VERSION = "provider-reconciliation-v1"
ELIGIBLE_PROVIDERS = ("alpaca_sip", "tiingo", "alpha_vantage")
DEFAULT_PRIORITY = ELIGIBLE_PROVIDERS


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Comparison thresholds and source preference, never a price-selection policy."""

    relative_tolerance: Decimal = Decimal("0.01")
    absolute_tolerance: Decimal = Decimal(0)
    provider_priority: tuple[str, ...] = DEFAULT_PRIORITY

    def __post_init__(self) -> None:
        if not self.relative_tolerance.is_finite() or not 0 <= self.relative_tolerance <= 1:
            raise ValueError("reconciliation relative tolerance must be between 0 and 1")
        if not self.absolute_tolerance.is_finite() or self.absolute_tolerance < 0:
            raise ValueError("reconciliation absolute tolerance must be nonnegative")
        if (
            not self.provider_priority
            or len(set(self.provider_priority)) != len(self.provider_priority)
            or set(self.provider_priority) != set(ELIGIBLE_PROVIDERS)
        ):
            raise ValueError("reconciliation provider priority must list each eligible provider once")

    def identity(self) -> dict:
        return {
            "rule_version": RECONCILIATION_RULE_VERSION,
            "relative_tolerance": str(self.relative_tolerance),
            "absolute_tolerance": str(self.absolute_tolerance),
            "provider_priority": list(self.provider_priority),
            "eligible_providers": list(ELIGIBLE_PROVIDERS),
            "financial_semantics": "US equity/raw_unadjusted/1d/USD/share/price",
        }


def normalize_reconciliation_policy(value: Mapping[str, object] | None = None) -> ReconciliationPolicy:
    if value is None:
        return ReconciliationPolicy()
    allowed = {"relative_tolerance", "absolute_tolerance", "provider_priority"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("Unknown reconciliation policy field(s): " + ", ".join(sorted(unknown)))
    defaults = ReconciliationPolicy()
    try:
        relative = Decimal(str(value.get("relative_tolerance", defaults.relative_tolerance)))
        absolute = Decimal(str(value.get("absolute_tolerance", defaults.absolute_tolerance)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Reconciliation tolerances must be finite decimals") from exc
    priority = value.get("provider_priority", defaults.provider_priority)
    if not isinstance(priority, (list, tuple)) or any(not isinstance(p, str) for p in priority):
        raise ValueError("provider_priority must be an ordered provider list")
    return ReconciliationPolicy(relative, absolute, tuple(priority))


def source_contract(row: Mapping[str, object]) -> dict | None:
    """Reject unknown semantics rather than silently assuming price equivalence."""

    if (
        row["provider"] not in ELIGIBLE_PROVIDERS
        or row["frequency"] != "1d"
        or row["adjustment"] != "raw_unadjusted"
    ):
        return None
    return {
        "instrument": row["symbol"],
        "provider": row["provider"],
        "session_date": row["session_date"],
        "frequency": "1d",
        "adjustment": "raw_unadjusted",
        "currency": "USD",
        "unit": "USD/share",
        "return_type": "price",
        "source_revision": {
            "retrieved_at": row.get("retrieved_at"),
            "ingestion_id": row.get("ingestion_id"),
        },
    }


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def coverage_bounds(path: Path) -> dict[int, dict]:
    """Bounded metadata: one min/max/count entry per series, never full price history."""

    conn = _read_connection(path, snapshot=False)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        rows = conn.execute(
            "SELECT series_id, MIN(session_date) AS first_session, "
            "MAX(session_date) AS last_session, COUNT(*) AS sessions "
            "FROM daily_prices GROUP BY series_id"
        ).fetchall()
        return {
            row["series_id"]: {
                "first_session": row["first_session"],
                "last_session": row["last_session"],
                "sessions": row["sessions"],
            }
            for row in rows
        }
    finally:
        conn.close()


def _observation(row: Mapping[str, object], contract: dict) -> dict:
    """Evidence contains stored fields and revisions, never URLs or credentials."""

    return {
        **contract,
        "series_id": row["series_id"],
        "close": str(row["close"]),
    }


def reconcile_session(
    observations: list[dict],
    *,
    series_group: list[Mapping[str, object]],
    bounds: Mapping[int, dict],
    policy: ReconciliationPolicy,
) -> list[dict]:
    """Produce deterministic candidates from one instrument/date/semantics group."""

    if not observations:
        return []
    # Delay importing quality to avoid a circular dependency at module import time.
    from finintel.market_data.quality import _candidate

    first = observations[0]
    if first["frequency"] != "1d" or first["adjustment"] != "raw_unadjusted":
        return []
    code = first["symbol"]
    session = first["session_date"]
    eligible = {
        r["provider"]: r
        for r in sorted(observations, key=lambda r: (r["provider"], r["series_id"]))
        if source_contract(r) is not None
    }
    series_by_provider = {
        s["provider"]: s
        for s in sorted(series_group, key=lambda s: (s["provider"], s["id"]))
        if s["provider"] in ELIGIBLE_PROVIDERS
        and s["frequency"] == "1d"
        and s["adjustment"] == "raw_unadjusted"
    }
    results = []
    for left, right in combinations(sorted(eligible), 2):
        lrow, rrow = eligible[left], eligible[right]
        left_contract, right_contract = source_contract(lrow), source_contract(rrow)
        if left_contract is None or right_contract is None:
            continue
        # Don't conflate instruments, units, adjustments or return conventions.
        dimensions = ("instrument", "session_date", "frequency", "adjustment",
                      "currency", "unit", "return_type")
        if any(left_contract[dim] != right_contract[dim] for dim in dimensions):
            continue
        a, b = _decimal(lrow["close"]), _decimal(rrow["close"])
        if a is None or b is None:
            continue
        absolute = abs(a - b)
        relative = absolute / a
        if absolute <= policy.absolute_tolerance or relative <= policy.relative_tolerance:
            continue
        provider_pair = f"{left}|{right}"
        results.append(
            _candidate(
                series_id=lrow["series_id"],
                code=code,
                provider=provider_pair,
                adjustment="raw_unadjusted",
                frequency="1d",
                session_date=session,
                finding_type="provider_divergence",
                severity="warning",
                confidence="low",
                # Retain PR #38's fingerprint identity and historical review state.
                rule="overlapping_close_differs_over_1pct",
                evidence={
                    "reconciliation_rule_version": RECONCILIATION_RULE_VERSION,
                    "candidate_only": True,
                    "left_provider": left,
                    "right_provider": right,
                    "left_close": str(a),
                    "right_close": str(b),
                    "relative_difference": str(relative),
                    "absolute_difference": str(absolute),
                    "adjustment": "raw_unadjusted",
                    "left_observation": _observation(lrow, left_contract),
                    "right_observation": _observation(rrow, right_contract),
                    "source_policy": policy.identity(),
                    "preferred_provider_for_future_analysis": next(
                        p for p in policy.provider_priority if p in (left, right)
                    ),
                    "automatic_price_selection": False,
                },
            )
        )
    # Only flag an absent provider inside its observed first/last boundaries.
    # Leading/trailing history may reflect entitlement or a partial backfill.
    for provider, series in sorted(series_by_provider.items()):
        if provider in eligible:
            continue
        extent = bounds.get(series["id"])
        if extent is None or not extent["first_session"] < session < extent["last_session"]:
            continue
        for present_provider, present in sorted(eligible.items()):
            pair = "|".join(sorted((provider, present_provider)))
            contract = source_contract(present)
            if contract is None:
                continue
            results.append(
                _candidate(
                    series_id=present["series_id"],
                    code=code,
                    provider=pair,
                    adjustment="raw_unadjusted",
                    frequency="1d",
                    session_date=session,
                    finding_type="provider_divergence",
                    severity="info",
                    confidence="medium",
                    rule="provider_session_missing_within_coverage_v1",
                    evidence={
                        "reconciliation_rule_version": RECONCILIATION_RULE_VERSION,
                        "candidate_only": True,
                        "missing_provider": provider,
                        "observed_provider": present_provider,
                        "observed": _observation(present, contract),
                        "missing_provider_coverage": extent,
                        "source_policy": policy.identity(),
                        "automatic_price_selection": False,
                    },
                )
            )
    return results


def reconciliation_summary(path: Path, *, code: str | None = None) -> dict:
    """Read-only, redacted status over existing findings and source coverage."""

    conn = _read_connection(path, snapshot=False)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        args = []
        where = ""
        if code is not None:
            where = " AND code = ?"
            args.append(code)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='data_quality_findings'"
        ).fetchone()
        findings = (
            conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN superseded_at IS NULL THEN 1 ELSE 0 END) AS active, "
                "SUM(CASE WHEN review_state <> 'candidate' THEN 1 ELSE 0 END) AS reviewed "
                "FROM data_quality_findings WHERE finding_type = 'provider_divergence'" + where,
                args,
            ).fetchone()
            if exists
            else {"total": 0, "active": 0, "reviewed": 0}
        )
        series_where = " WHERE symbol = ?" if code is not None else ""
        coverage = conn.execute(
            "SELECT provider, COUNT(*) AS series_count FROM series"
            + series_where + " GROUP BY provider ORDER BY provider",
            args,
        ).fetchall()
        details = []
        if code is not None:
            series_coverage = conn.execute(
                "SELECT s.provider, s.frequency, s.adjustment, "
                "MIN(d.session_date) AS first_session, "
                "MAX(d.session_date) AS last_session, "
                "COUNT(d.session_date) AS sessions "
                "FROM series AS s LEFT JOIN daily_prices AS d ON d.series_id = s.id "
                "WHERE s.symbol = ? "
                "GROUP BY s.id ORDER BY s.provider, s.frequency, s.adjustment",
                (code,),
            ).fetchall()
            details = [dict(row) for row in series_coverage]
        return {
            "scope": code or "all",
            "coverage_by_series": details,
            "findings": {
                "total": findings["total"],
                "active": findings["active"] or 0,
                "reviewed": findings["reviewed"] or 0,
            },
            "provider_series": [
                {"provider": row["provider"], "series_count": row["series_count"]}
                for row in coverage
            ],
            "eligibility": {
                "eligible_providers": list(ELIGIBLE_PROVIDERS),
                "price_semantics": "US equity daily raw_unadjusted USD/share",
                "unsupported_series": "excluded, not converted or matched",
            },
        }
    finally:
        conn.close()
