"""AL-02 numerical and source evidence tests on disposable local SQLite data."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Context, Decimal, localcontext

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finintel.main import app
from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_basket import run_index_basket
from finintel.market_data.analytics_store import _read_series, run_analytics
from finintel.models.contracts import (
    AnalysisRequest,
    AnalysisResult,
    DateRange,
    SourceIdentity,
    SourcePin,
)
from finintel.models.registry import (
    PRICE_INDEX_SOURCES,
    analysis_model_registry,
    get_model_definition,
    model_registry,
)
from finintel.models.trend import evaluate_trend_model
from finintel.storage.db import _read_connection, _write_connection

START = date(2018, 1, 2)
TIMESTAMP = datetime(2025, 2, 3, tzinfo=UTC)


def _day(offset):
    return START + timedelta(days=offset)


def _identity(code):
    return next(identity for identity in PRICE_INDEX_SOURCES if identity.code == code)


def _seed(db, values, *, code="SP500", offsets=None):
    offsets = range(len(values)) if offsets is None else offsets
    conn = _write_connection(db)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO index_daily_closes "
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "VALUES (?,'fred',?,?,'2025-01-01T00:00:00+00:00','synthetic fixture')",
                [
                    (code, _day(offset).isoformat(), str(value))
                    for offset, value in zip(offsets, values, strict=True)
                ],
            )
    finally:
        conn.close()


def _request(
    db,
    model,
    *,
    codes=("SP500",),
    end=START + timedelta(days=365),
    parameters=None,
    quality_policy="allow_unverified",
):
    conn = _read_connection(db)
    try:
        pins = []
        for code in codes:
            _, _, provenance = _read_series(
                conn, kind="index", code=code, provider="fred", start=START, end=end
            )
            pins.append(
                SourcePin(identity=_identity(code), snapshot_sha256=provenance["snapshot_sha256"])
            )
    finally:
        conn.close()
    return AnalysisRequest(
        model_id=f"builtin.{model}",
        model_version="1.0.0",
        sources=tuple(pins),
        window=DateRange(start=START, end=end),
        parameters=parameters or {},
        quality_policy=quality_policy,
    )


def _evaluate(db, model, **kwargs):
    return evaluate_trend_model(_request(db, model, **kwargs), db=db, evaluated_at_utc=TIMESTAMP)


def test_ema_seed_equality_crossover_and_prefix_invariance(tmp_path):
    db = tmp_path / "ema.sqlite"
    _seed(db, [10, 10, 10, 14, 14, 8, 8, 14])
    parameters = {"fast_window": 1, "slow_window": 3}
    full = _evaluate(db, "ema_trend", parameters=parameters)
    prefix = _evaluate(db, "ema_trend", parameters=parameters, end=_day(4))
    assert prefix.points == full.points[:5]
    assert [point["warmup_complete"] for point in full.points] == [False, False] + [True] * 6
    assert [point["slow_ema"] for point in full.points[:2]] == [None, None]
    assert [Decimal(point["slow_ema"]) for point in full.points[2:]] == [
        Decimal(level) for level in ("10", "12", "13", "10.5", "9.25", "11.625")
    ]
    assert [point["ema_difference"] for point in full.points[:2]] == [None, None]
    assert Decimal(full.points[5]["ema_difference"]) == Decimal("-2.5")
    assert [point["trend_after_close"] for point in full.points] == [
        None,
        None,
        "flat",
        "above",
        "above",
        "below",
        "below",
        "above",
    ]
    assert [point["crossover_after_close"] for point in full.points] == [
        None,
        None,
        "none",
        "above",
        "none",
        "below",
        "none",
        "above",
    ]


@pytest.mark.parametrize(
    ("levels", "state", "fast_seed", "slow_seed"),
    [([2, 4, 6, 8], "above", 3, 4), ([8, 6, 4, 2], "below", 7, 6), ([5] * 4, "flat", 5, 5)],
)
def test_ema_arithmetic_seed_and_first_comparable_is_not_cross(
    tmp_path, levels, state, fast_seed, slow_seed
):
    db = tmp_path / "ema.sqlite"
    _seed(db, levels)
    result = _evaluate(db, "ema_trend", parameters={"fast_window": 2, "slow_window": 3})
    assert result.points[0]["fast_ema"] is None
    assert Decimal(result.points[1]["fast_ema"]) == fast_seed
    assert Decimal(result.points[2]["slow_ema"]) == slow_seed
    assert result.points[2]["crossover_after_close"] == "none"
    assert result.points[2]["trend_after_close"] == result.points[3]["trend_after_close"] == state


def test_ema_constant_large_decimal_stays_flat_and_window_one_tracks_close(tmp_path):
    db = tmp_path / "ema-edge.sqlite"
    level = "1234567890123456789012345678"
    _seed(db, [level] * 26 + ["1"])
    result = _evaluate(db, "ema_trend")
    assert result.points[25]["trend_after_close"] == "flat"
    assert Decimal(result.points[25]["fast_ema"]) == Decimal(level)
    assert Decimal(result.points[25]["slow_ema"]) == Decimal(level)
    fast = _evaluate(db, "ema_trend", parameters={"fast_window": 1, "slow_window": 26})
    assert fast.points[-1]["fast_ema"] == "1"
    assert fast.points[-1]["crossover_after_close"] == "below"


@pytest.mark.parametrize("end_level,expected", [(125, "0.25"), (75, "-0.25"), (100, "0")])
def test_momentum_signed_return_at_exact_observed_lookback(tmp_path, end_level, expected):
    db = tmp_path / "momentum.sqlite"
    offsets = [2 * index for index in range(22)]
    _seed(db, [100] * 21 + [end_level], offsets=offsets)
    parameters = {"lookback_sessions": 21}
    result = _evaluate(db, "momentum", parameters=parameters)
    assert [point["session_date"] for point in result.points] == [
        _day(offset).isoformat() for offset in offsets
    ]
    assert all(
        point["momentum"] is None and not point["warmup_complete"] for point in result.points[:-1]
    )
    assert result.points[-1] == {
        "session_date": _day(42).isoformat(),
        "close": str(end_level),
        "comparison_session_date": START.isoformat(),
        "comparison_close": "100",
        "momentum": expected,
        "lookback_sessions": 21,
        "warmup_complete": True,
    }
    with pytest.raises(AnalyticsError, match="At least 22 observed"):
        _evaluate(db, "momentum", parameters=parameters, end=_day(40))


def test_precision_and_canonical_hashes_do_not_depend_on_ambient_context(tmp_path):
    db = tmp_path / "precision.sqlite"
    _seed(db, [3] * 21 + [4])
    request = _request(db, "momentum", parameters={"lookback_sessions": 21})
    canonical = evaluate_trend_model(request, db=db, evaluated_at_utc=TIMESTAMP)
    assert canonical.points[-1]["momentum"] == "0.333333333333333333333333333"
    assert (
        canonical.output_sha256
        == "40854dcbe37310c4213c0b3e0069058a3066f3c112845ea9b0edebffc4ad7486"
    )
    assert (
        canonical.reproducibility_sha256
        == "c0af14fdb90204c50cfbc11a1cc4e5a6ee03ff0c86fc176700e8f478e834efd3"
    )
    with localcontext(Context(prec=8, rounding=ROUND_DOWN)):
        other = evaluate_trend_model(request, db=db, evaluated_at_utc=TIMESTAMP + timedelta(days=1))
    assert other.points == canonical.points
    assert other.output_sha256 == canonical.output_sha256
    assert other.reproducibility_sha256 == canonical.reproducibility_sha256
    assert other.evaluated_at_utc != canonical.evaluated_at_utc
    decoded = AnalysisResult.model_validate_json(canonical.model_dump_json())
    assert decoded == canonical
    get_model_definition(request).validate_result(request, decoded)


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        ("ema_trend", {"fast_window": 0}),
        ("ema_trend", {"fast_window": True}),
        ("ema_trend", {"fast_window": 26}),
        ("ema_trend", {"slow_window": 2001}),
        ("momentum", {"lookback_sessions": 20}),
        ("momentum", {"lookback_sessions": "21"}),
        ("momentum", {"days": 21}),
        ("relative_strength", {"benchmark": "NDX"}),
        ("rsi_state", {"window": 0}),
        ("rsi_state", {"window": 2000}),
        ("rsi_state", {"lower_threshold": 70, "upper_threshold": 70}),
        ("rsi_state", {"upper_threshold": 101}),
    ],
)
def test_strict_parameter_contracts(tmp_path, model, parameters):
    db = tmp_path / "parameters.sqlite"
    _seed(db, [100, 101])
    codes = ("SP500", "DJIA") if model == "relative_strength" else ("SP500",)
    request = _request(db, model, codes=codes, parameters=parameters)
    with pytest.raises(ValidationError):
        evaluate_trend_model(request, db=db)


def test_relative_strength_common_dates_ratios_return_differences_and_order(tmp_path):
    db = tmp_path / "relative.sqlite"
    _seed(db, [100, 110, 125, 130], offsets=[0, 1, 2, 4])
    _seed(db, [200, 225, 250], code="DJIA", offsets=[0, 2, 4])
    _seed(db, [400, 500, 520], code="NDX", offsets=[0, 2, 4])
    _seed(db, [100, 100, 200], code="NASDAQCOM", offsets=[0, 3, 4])
    codes = ("SP500", "DJIA", "NDX")
    result = _evaluate(db, "relative_strength", codes=codes)
    assert tuple(source.identity.code for source in result.evidence.sources) == codes
    assert [source.aligned_count for source in result.evidence.sources] == [3, 3, 3]
    assert [source.excluded_count for source in result.evidence.sources] == [1, 0, 0]
    assert result.evidence.observed_sessions == (START, _day(2), _day(4))
    baseline, second, last = result.points
    assert baseline["benchmark"] == "sp500"
    assert Decimal(baseline["djia_normalized_100"]) == 100
    assert Decimal(baseline["djia_relative_strength"]) == 1
    assert Decimal(baseline["djia_return_difference"]) == 0
    assert baseline["nasdaqcom_normalized_100"] is None
    assert Decimal(second["djia_relative_strength"]) == Decimal("0.9")
    assert Decimal(second["djia_return_difference"]) == Decimal("-0.125")
    assert Decimal(last["ndx_relative_strength"]) == 1
    assert Decimal(last["ndx_return_difference"]) == 0
    reversed_result = _evaluate(db, "relative_strength", codes=("DJIA", "SP500", "NDX"))
    assert reversed_result.points[1]["benchmark"] == "djia"
    assert reversed_result.points[1]["sp500_relative_strength"] == "1.111111111111111111111111111"
    assert Decimal(reversed_result.points[1]["sp500_return_difference"]) == Decimal("0.125")
    assert reversed_result.reproducibility_sha256 != result.reproducibility_sha256
    basket = run_index_basket(
        codes=list(codes), start=START, end=_day(4), quality_policy="allow_unverified", db=db
    )
    assert (
        basket["points"][1]["values"]["DJIA"]["relative_strength"]
        == (reversed_result.points[1]["sp500_relative_strength"])
    )
    assert (
        basket["points"][1]["values"]["DJIA"]["normalized_100"]
        == (result.points[1]["djia_normalized_100"])
    )
    four = _evaluate(db, "relative_strength", codes=(*codes, "NASDAQCOM"))
    assert four.evidence.observed_sessions == (START, _day(4))
    assert four.points[1]["nasdaqcom_normalized_100"] == "200"


@pytest.mark.parametrize("offsets", [[3, 4], [2, 3]])
def test_relative_strength_rejects_empty_and_one_session_intersections(tmp_path, offsets):
    db = tmp_path / "missing.sqlite"
    _seed(db, [100, 101], offsets=[0, 2])
    _seed(db, [200, 210], code="DJIA", offsets=offsets)
    with pytest.raises(AnalyticsError, match="two|2 shared"):
        _evaluate(db, "relative_strength", codes=("SP500", "DJIA"))


def test_relative_snapshot_revisions_are_source_specific(tmp_path):
    db = tmp_path / "revisions.sqlite"
    _seed(db, [100, 120])
    _seed(db, [200, 220], code="DJIA")
    codes = ("SP500", "DJIA")
    request = _request(db, "relative_strength", codes=codes)
    first = evaluate_trend_model(request, db=db)
    conn = _write_connection(db)
    try:
        with conn:
            conn.execute(
                "UPDATE index_daily_closes SET retrieved_at=? WHERE index_code='DJIA'",
                ("2025-01-02T00:00:00+00:00",),
            )
    finally:
        conn.close()
    with pytest.raises(AnalyticsError, match="snapshot changed"):
        evaluate_trend_model(request, db=db)
    latest = _evaluate(db, "relative_strength", codes=codes)
    assert first.evidence.sources[0].snapshot_sha256 == latest.evidence.sources[0].snapshot_sha256
    assert first.evidence.sources[1].snapshot_sha256 != latest.evidence.sources[1].snapshot_sha256
    assert first.output_sha256 == latest.output_sha256
    assert first.reproducibility_sha256 != latest.reproducibility_sha256


@pytest.mark.parametrize(
    ("levels", "expected", "state"),
    [
        ([1, 2, 3], "100", "high"),
        ([3, 2, 1], "0", "low"),
        ([5, 5, 5], "50", "neutral"),
        ([5, 8, 7], "75", "high"),
        ([5, 6, 3], "25", "low"),
    ],
)
def test_rsi_simple_gains_losses_and_inclusive_thresholds(tmp_path, levels, expected, state):
    db = tmp_path / "rsi.sqlite"
    _seed(db, levels)
    parameters = {"window": 2, "lower_threshold": 25, "upper_threshold": 75}
    result = _evaluate(db, "rsi_state", parameters=parameters)
    assert [point["rsi"] for point in result.points[:-1]] == [None, None]
    assert [point["warmup_complete"] for point in result.points] == [False, False, True]
    assert result.points[-1]["rsi"] == expected
    assert result.points[-1]["rsi_state"] == state
    assert result.points[-1]["lower_threshold"] == 25
    assert result.points[-1]["upper_threshold"] == 75


def test_rsi_default_matches_analytics_and_short_history_stays_null(tmp_path):
    db = tmp_path / "rsi.sqlite"
    _seed(db, [100 + offset + (offset % 3) * 4 for offset in range(21)])
    old = run_analytics(
        start=START, end=_day(20), window=14, quality_policy="allow_unverified", db=db
    )
    result = _evaluate(db, "rsi_state", end=_day(20))
    assert result.evidence.parameters["window"] == 14
    assert [point["rsi"] for point in result.points] == [point["rsi"] for point in old["points"]]
    short = _evaluate(db, "rsi_state", end=_day(0))
    assert len(short.points) == 1
    assert short.points[0]["rsi"] is short.points[0]["rsi_state"] is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1", "0", "1E+999999"])
def test_invalid_or_unrepresentable_local_prices_fail(tmp_path, value):
    db = tmp_path / "invalid.sqlite"
    _seed(db, [value])
    with pytest.raises(AnalyticsError, match="finite and positive|representation limit"):
        _evaluate(db, "rsi_state")


@pytest.mark.parametrize(
    "field,value", [("provider", "other"), ("adjustment", "total_return"), ("code", "VIX")]
)
def test_unsupported_sources_are_rejected_by_strict_identity(field, value):
    with pytest.raises(ValidationError):
        SourceIdentity(**(_identity("SP500").model_dump() | {field: value}))


def test_source_count_cap_is_applied_before_relative_alignment(tmp_path):
    db = tmp_path / "limit.sqlite"
    _seed(db, [100] * 2001)
    _seed(db, [200, 210], code="DJIA")
    request = _request(db, "relative_strength", codes=("SP500", "DJIA"), end=_day(2000))
    with pytest.raises(AnalyticsError, match="exceeds 2,000"):
        evaluate_trend_model(request, db=db)


def test_quality_gate_override_and_database_unchanged_without_network(tmp_path, monkeypatch):
    db = tmp_path / "read-only.sqlite"
    _seed(db, [100, 110])
    request = _request(db, "rsi_state", quality_policy="require_complete")
    with pytest.raises(AnalyticsError, match="complete Data Quality"):
        evaluate_trend_model(request, db=db)

    def no_network(*args, **kwargs):
        pytest.fail("Model evaluation attempted a network request")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    before = db.read_bytes()
    result = _evaluate(db, "rsi_state")
    assert result.evidence.warnings == ("quality_override",)
    assert result.evidence.sources[0].quality.status == "not_run"
    assert result.evidence.sources[0].quality.warnings == ("quality_scan_not_run",)
    legacy = run_analytics(start=START, end=_day(1), quality_policy="allow_unverified", db=db)
    assert legacy["quality"]["status"] == "stale_or_partial"
    assert result.evidence.research_only is True
    assert db.read_bytes() == before


def test_registry_exposes_new_proposed_models_without_managed_execution(monkeypatch):
    legacy = model_registry()["models"]
    catalog = analysis_model_registry()["models"]
    assert [model["model_id"] for model in legacy] == ["builtin.sma_crossover"]
    assert [model["model_id"] for model in catalog] == [
        "builtin.sma_crossover",
        "builtin.ema_trend",
        "builtin.momentum",
        "builtin.relative_strength",
        "builtin.rsi_state",
        "builtin.risk_profile",
        "builtin.market_regime",
    ]
    assert [model["availability"] for model in catalog] == ["released"] + ["proposed"] * 6
    token = "trend-model-contract-test-token-32bytes"
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    with TestClient(app, headers={"x-finintel-process-token": token}) as client:
        assert client.get("/api/v1/models/registry/v2").json()["models"] == catalog
        for model in catalog[1:]:
            for route in ("/api/v1/models/evaluate", "/api/v1/models/jobs"):
                response = client.post(
                    route,
                    json={
                        "model_id": model["model_id"],
                        "start": START.isoformat(),
                        "end": START.isoformat(),
                    },
                )
                assert response.status_code == 422


def _record_quality_scan(db, *, status, stale=0, reference_date=None):
    reference_date = reference_date or datetime.now(UTC).date().isoformat()
    conn = _write_connection(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO data_quality_scan_progress "
                "(scan_key,source_identity,source_data_version_start,source_data_version_end,"
                "cursor_start,cursor_next,max_findings,findings_examined,findings_stored,"
                "total_known_findings,persisted_findings_total,status,stale,updated_at,"
                "validation_reference_date) "
                "SELECT 'all','fixture',source_version,source_version,0,0,1,0,0,0,0,"
                "?,?,?,? FROM data_quality_source_state WHERE id=1",
                (status, stale, TIMESTAMP.isoformat(), reference_date),
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("scan_status", "stale", "old_reference", "expected"),
    [
        ("partial", 0, False, "partial"),
        ("partial", 1, False, "stale"),
        ("partial", 0, True, "stale"),
        ("stale", 0, False, "stale"),
    ],
)
def test_partial_and_stale_scan_evidence_preserves_legacy_status(
    tmp_path, scan_status, stale, old_reference, expected
):
    db = tmp_path / "quality-status.sqlite"
    _seed(db, [100, 101])
    _record_quality_scan(
        db,
        status=scan_status,
        stale=stale,
        reference_date=TIMESTAMP.date().isoformat() if old_reference else None,
    )
    result = _evaluate(db, "rsi_state", end=_day(1))
    assert result.evidence.sources[0].quality.status == expected
    assert result.evidence.sources[0].quality.warnings == (f"quality_scan_{expected}",)
    legacy = run_analytics(start=START, end=_day(1), quality_policy="allow_unverified", db=db)
    assert legacy["quality"] == {
        "status": "stale_or_partial",
        "quality_verified": False,
        "active_unresolved_errors": 0,
        "active_unresolved_findings": 0,
        "scan_complete_current": False,
    }
    with pytest.raises(AnalyticsError, match="complete Data Quality"):
        _evaluate(db, "rsi_state", end=_day(1), quality_policy="require_complete")


def test_current_complete_scan_verifies_each_source_under_both_policies(tmp_path):
    db = tmp_path / "verified.sqlite"
    _seed(db, [100, 105])
    _seed(db, [200, 210], code="NDX")
    _record_quality_scan(db, status="completed")

    codes = ("SP500", "NDX")
    verified = _evaluate(db, "relative_strength", codes=codes, quality_policy="require_complete")
    override = _evaluate(db, "relative_strength", codes=codes)
    assert [source.quality.status for source in verified.evidence.sources] == ["verified"] * 2
    assert verified.evidence.warnings == ()
    legacy = run_analytics(start=START, end=_day(1), db=db)
    assert legacy["quality"]["status"] == "complete"
    assert legacy["quality"]["quality_verified"] is True
    assert override.evidence.sources == verified.evidence.sources
    assert override.evidence.warnings == ("quality_override",)
    assert override.output_sha256 == verified.output_sha256
    assert override.reproducibility_sha256 != verified.reproducibility_sha256


def test_source_dates_are_revalidated_after_pin_match(tmp_path):
    db = tmp_path / "invalid-date.sqlite"
    _seed(db, [100, 110])
    conn = _write_connection(db)
    try:
        with conn:
            conn.execute(
                "UPDATE index_daily_closes SET session_date='2018-01-031' "
                "WHERE session_date='2018-01-03'"
            )
    finally:
        conn.close()
    with pytest.raises(AnalyticsError, match="Invalid market session date"):
        _evaluate(db, "rsi_state")


def test_validated_request_rejects_duplicate_source_and_unknown_model_version(tmp_path):
    db = tmp_path / "allowlist.sqlite"
    _seed(db, [100])
    request = _request(db, "rsi_state")
    duplicate = request.model_copy(update={"sources": request.sources * 2})
    unsupported = request.model_copy(update={"model_version": "2.0.0"})
    with pytest.raises(ValidationError, match="Duplicate analysis source identity"):
        evaluate_trend_model(duplicate, db=db)
    with pytest.raises(ValueError, match="Unknown analysis model ID or version"):
        evaluate_trend_model(unsupported, db=db)
