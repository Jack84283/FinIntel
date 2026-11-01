"""No silent replacement, incompatible comparisons, or unstable finding identities."""

from decimal import Decimal

import pytest

from finintel.market_data.reconciliation import (
    ReconciliationPolicy,
    normalize_reconciliation_policy,
    reconcile_session,
    source_contract,
)


def row(provider, close="100", session="2026-10-21", adjustment="raw_unadjusted"):
    return {
        "series_id": {"alpaca_sip": 1, "tiingo": 2, "alpha_vantage": 3}[provider],
        "symbol": "AAPL",
        "provider": provider,
        "frequency": "1d",
        "adjustment": adjustment,
        "session_date": session,
        "close": close,
        "retrieved_at": "2026-10-22T12:00:00+00:00",
        "ingestion_id": 100,
    }


def catalog(*providers):
    return [
        {
            "id": row(p)["series_id"],
            "symbol": "AAPL",
            "provider": p,
            "frequency": "1d",
            "adjustment": "raw_unadjusted",
        }
        for p in providers
    ]


def scan(observations, *providers, policy=None, bounds=None):
    return reconcile_session(
        observations,
        series_group=catalog(*providers),
        bounds=bounds or {},
        policy=policy or ReconciliationPolicy(),
    )


def test_same_values_are_compatible_and_have_no_findings():
    observations = [row("alpaca_sip"), row("tiingo")]
    assert source_contract(observations[0])["currency"] == "USD"
    assert not scan(observations, "alpaca_sip", "tiingo")


def test_material_difference_has_two_stored_observations_and_stable_fingerprint():
    observations = [row("alpaca_sip"), row("tiingo", "120")]
    found = scan(observations, "alpaca_sip", "tiingo")
    assert len(found) == 1
    finding = found[0]
    assert finding["finding_type"] == "provider_divergence"
    assert finding["rule"] == "overlapping_close_differs_over_1pct"
    assert finding["confidence"] == "low"
    assert finding["evidence"]["left_observation"]["close"] == "100"
    assert finding["evidence"]["right_observation"]["close"] == "120"
    assert finding["evidence"]["left_observation"]["source_revision"]["ingestion_id"] == 100
    assert finding["evidence"]["automatic_price_selection"] is False
    assert scan(observations[::-1], "alpaca_sip", "tiingo")[0]["fingerprint"] == finding["fingerprint"]


def test_tolerance_is_configurable_without_moving_finding_identity():
    observations = [row("alpaca_sip"), row("tiingo", "101.5")]
    assert len(scan(observations, "alpaca_sip", "tiingo")) == 1
    policy = ReconciliationPolicy(relative_tolerance=Decimal("0.02"))
    assert not scan(observations, "alpaca_sip", "tiingo", policy=policy)
    reordered = ReconciliationPolicy(
        provider_priority=("tiingo", "alpaca_sip", "alpha_vantage")
    )
    a = scan(observations, "alpaca_sip", "tiingo")[0]
    b = scan(observations, "alpaca_sip", "tiingo", policy=reordered)[0]
    assert a["fingerprint"] == b["fingerprint"]
    assert b["evidence"]["preferred_provider_for_future_analysis"] == "tiingo"


def test_unsupported_adjustment_does_not_silently_compare():
    adjusted = row("alpaca_sip", adjustment="split_adjusted")
    assert source_contract(adjusted) is None
    assert not scan([adjusted, row("tiingo")], "alpaca_sip", "tiingo")


def test_unknown_provider_and_frequency_refused():
    other = row("alpaca_sip")
    other["provider"] = "unknown_feed"
    assert source_contract(other) is None
    weekly = row("alpaca_sip")
    weekly["frequency"] = "1wk"
    assert source_contract(weekly) is None


def test_interior_missing_session_only_when_inside_verified_coverage():
    bounds = {
        2: {
            "first_session": "2026-10-19",
            "last_session": "2026-10-23",
            "sessions": 4,
        }
    }
    found = scan([row("alpaca_sip")], "alpaca_sip", "tiingo", bounds=bounds)
    assert len(found) == 1
    finding = found[0]
    assert finding["rule"] == "provider_session_missing_within_coverage_v1"
    assert finding["evidence"]["missing_provider"] == "tiingo"
    assert finding["evidence"]["observed_provider"] == "alpaca_sip"
    assert finding["evidence"]["candidate_only"]
    assert not scan(
        [row("alpaca_sip", session="2026-10-23")],
        "alpaca_sip",
        "tiingo",
        bounds=bounds,
    )


def test_absent_provider_with_no_coverage_is_not_flagged():
    assert not scan([row("alpaca_sip")], "alpaca_sip", "tiingo")


def test_finite_thresholds_and_complete_priority_are_required():
    for override in (
        {"relative_tolerance": "NaN"},
        {"relative_tolerance": "-1"},
        {"absolute_tolerance": "Infinity"},
        {"provider_priority": ["alpaca_sip", "tiingo", "tiingo"]},
        {"provider_priority": ["alpaca_sip"]},
        {"typo": "hello"},
    ):
        with pytest.raises(ValueError):
            normalize_reconciliation_policy(override)
    assert normalize_reconciliation_policy().relative_tolerance == Decimal("0.01")
