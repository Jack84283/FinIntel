"""Historical risk fixtures with disposable, synthetic local price snapshots."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from pydantic import ValidationError

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_store import _read_series
from finintel.models.contracts import AnalysisRequest, AnalysisResult, DateRange, SourcePin
from finintel.models.registry import PRICE_INDEX_SOURCES, RISK_MODEL_ID, RiskParameters
from finintel.models.risk import _historical_tail, evaluate_risk_model
from finintel.storage.db import _read_connection, _write_connection

_START = date(2024, 1, 2)
_EVALUATED = datetime(2025, 1, 1, tzinfo=UTC)


def _date(offset):
    return _START + timedelta(days=offset)


def _seed(db, prices, *, code="SP500", offsets=None):
    offsets = range(len(prices)) if offsets is None else offsets
    conn = _write_connection(db)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO index_daily_closes "
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "VALUES (?,'fred',?,?,'2025-01-01T00:00:00+00:00','synthetic fixture')",
                [
                    (code, _date(offset).isoformat(), str(price))
                    for offset, price in zip(offsets, prices, strict=True)
                ],
            )
    finally:
        conn.close()


def _request(db, *, codes=("SP500",), end=100, parameters=None):
    conn = _read_connection(db)
    try:
        pins = []
        for code in codes:
            _, _, provenance = _read_series(
                conn, kind="index", code=code, provider="fred", start=_START, end=_date(end)
            )
            identity = next(source for source in PRICE_INDEX_SOURCES if source.code == code)
            pins.append(SourcePin(identity=identity, snapshot_sha256=provenance["snapshot_sha256"]))
    finally:
        conn.close()
    return AnalysisRequest(
        model_id=RISK_MODEL_ID,
        model_version="1.0.0",
        sources=tuple(pins),
        window=DateRange(start=_START, end=_date(end)),
        parameters=parameters or {},
        quality_policy="allow_unverified",
    )


def _evaluate(db, **kwargs):
    return evaluate_risk_model(_request(db, **kwargs), db=db, evaluated_at_utc=_EVALUATED)


def test_aligned_volatility_downside_and_correlation(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100, 500, 110, 99], offsets=[0, 1, 3, 7])
    _seed(db, [100, 90, 400, 99], code="NDX", offsets=[0, 3, 4, 7])
    result = _evaluate(db, codes=("SP500", "NDX"), parameters={"risk_window": 2})
    first, warmup, current = result.points
    assert [point["session_date"] for point in result.points] == [
        _date(offset).isoformat() for offset in [0, 3, 7]
    ]
    assert first["simple_return"] is None
    assert Decimal(warmup["simple_return"]) == Decimal("0.1")
    assert warmup["annualized_volatility"] is None
    assert Decimal(current["simple_return"]) == Decimal("-0.1")
    assert Decimal(current["comparison_return"]) == Decimal("0.1")
    # Sample variance of [0.1, -0.1] is 0.02. Downside uses both returns.
    assert Decimal(current["annualized_volatility"]) == pytest.approx(Decimal("5.04").sqrt())
    assert Decimal(current["downside_deviation"]) == pytest.approx(Decimal("0.005").sqrt())
    assert Decimal(current["correlation"]) == -1
    assert [
        (source.aligned_count, source.excluded_count) for source in result.evidence.sources
    ] == [
        (3, 1),
        (3, 1),
    ]


def test_drawdown_episodes_and_as_of_recovery(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100, 100, 75, 75, 100, 90, 80])
    result = _evaluate(db, parameters={"risk_window": 2})
    prefix = _evaluate(db, end=3, parameters={"risk_window": 2})
    assert prefix.points == result.points[:4]
    open_first = result.points[3]
    assert open_first["open_peak_date"] == _date(1).isoformat()
    assert open_first["open_trough_date"] == _date(2).isoformat()
    assert open_first["open_elapsed_intervals"] == 2
    assert open_first["last_recovery_date"] is None
    recovered = result.points[4]
    assert Decimal(recovered["current_drawdown"]) == 0
    assert recovered["open_peak_date"] is None
    assert recovered["last_recovery_date"] == _date(4).isoformat()
    assert recovered["last_peak_to_recovery_intervals"] == 3
    assert recovered["last_trough_to_recovery_intervals"] == 2
    final = result.points[-1]
    assert Decimal(final["maximum_drawdown"]) == Decimal("-0.25")
    assert Decimal(final["current_drawdown"]) == Decimal("-0.2")
    assert final["maximum_peak_date"] == _date(1).isoformat()
    assert final["open_trough_date"] == _date(6).isoformat()
    assert final["last_recovery_date"] == _date(4).isoformat()


def test_var_interpolation_does_not_include_lower_rank_loss():
    returns = [Decimal(0)] * 56 + list(map(Decimal, ("-.04", "-.08", "-.10", "-.20")))
    result = _historical_tail(returns, 60)
    # Rank 56.05 lies 5% of the way from loss .04 to loss .08.
    assert Decimal(result["var_95"]) == Decimal(".042")
    assert result["historical_sample_count"] == 60
    assert result["historical_tail_count"] == 3
    assert Decimal(result["expected_shortfall_95"]) == pytest.approx(Decimal(".38") / 3)


@pytest.mark.parametrize(
    ("returns", "var", "es", "tail_count", "status"),
    [
        (["0"] * 56 + ["-.05"] * 4, ".05", ".05", 4, "available"),
        ([".01"] * 59 + ["0"], "0", "0", 1, "available"),
        ([".01"] * 59 + ["-.2"], "0", ".2", 1, "available"),
        ([".01"] * 60, "0", None, 0, "empty_tail"),
        (["0"] * 59, None, None, None, "insufficient_history"),
    ],
)
def test_tail_ties_zero_floor_empty_and_insufficient(returns, var, es, tail_count, status):
    result = _historical_tail(list(map(Decimal, returns)), 60)
    assert result["historical_sample_count"] == len(returns)
    assert result["historical_tail_count"] == tail_count
    assert result["historical_tail_status"] == status
    assert (result["var_95"] is None) == (var is None)
    assert (result["expected_shortfall_95"] is None) == (es is None)
    if var is not None:
        assert Decimal(result["var_95"]) == Decimal(var)
    if es is not None:
        assert Decimal(result["expected_shortfall_95"]) == Decimal(es)


def test_tail_window_prefix_hash_precision_and_zero_variance(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100, 50] + [50] * 60)
    _seed(db, [200] * 62, code="NDX")
    parameters = {"tail_window": 60, "risk_window": 2}
    request = _request(db, codes=("SP500", "NDX"), parameters=parameters)
    before = db.read_bytes()
    result = evaluate_risk_model(request, db=db, evaluated_at_utc=_EVALUATED)
    with localcontext() as context:
        context.prec = 8
        context.rounding = ROUND_DOWN
        later = evaluate_risk_model(request, db=db, evaluated_at_utc=_EVALUATED + timedelta(days=1))
    assert result.output_sha256 == later.output_sha256
    assert (
        result.output_sha256 == "c0e4766d70a7a047dd5ab5a1886a0c6dad5ff25b731ceaa38e25e8c843993f44"
    )
    assert result.reproducibility_sha256 == later.reproducibility_sha256
    assert (
        result.reproducibility_sha256
        == "10da5b1a67ad5c5ccad906c1e5219c753dc41421ed69dfaf1edc74bdbbe02b61"
    )
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result
    assert db.read_bytes() == before
    prefix = _evaluate(db, codes=("SP500", "NDX"), end=60, parameters=parameters)
    assert prefix.points == result.points[:61]
    assert result.points[59]["historical_tail_status"] == "insufficient_history"
    assert result.points[60]["historical_sample_count"] == 60
    assert Decimal(result.points[60]["expected_shortfall_95"]) == Decimal("0.5") / 60
    assert result.points[61]["historical_tail_count"] == 60
    assert Decimal(result.points[61]["expected_shortfall_95"]) == 0
    assert result.points[60]["correlation"] is None


def test_tail_statistics_require_the_full_requested_window(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100] * 253)
    result = _evaluate(db, end=252, parameters={"tail_window": 252})
    for count in (60, 251):
        point = result.points[count]
        assert point["historical_sample_count"] == count
        assert point["historical_tail_status"] == "insufficient_history"
        assert point["historical_tail_count"] is None
        assert point["var_95"] is None
        assert point["expected_shortfall_95"] is None

    ready = result.points[252]
    assert ready["historical_sample_count"] == 252
    assert ready["historical_tail_status"] == "available"
    assert ready["historical_tail_count"] == 252
    assert Decimal(ready["var_95"]) == 0
    assert Decimal(ready["expected_shortfall_95"]) == 0


def test_rejects_changed_pin_unsupported_source_and_missing_overlap(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100, 101])
    _seed(db, [90, 91], code="NDX", offsets=[3, 4])
    request = _request(db)
    assert _evaluate(db).evidence.sources[0].quality.status == "not_run"
    with pytest.raises(AnalyticsError, match="complete Data Quality"):
        evaluate_risk_model(
            request.model_copy(update={"quality_policy": "require_complete"}), db=db
        )
    with pytest.raises(AnalyticsError, match="shared observed session"):
        _evaluate(db, codes=("SP500", "NDX"))
    pin = request.sources[0]
    volatility_index = pin.identity.model_copy(
        update={"code": "VIX", "return_semantics": "volatility_index"}
    )
    wrong_pin = pin.model_copy(update={"identity": volatility_index})
    with pytest.raises(ValidationError, match="VIX"):
        evaluate_risk_model(request.model_copy(update={"sources": (wrong_pin,)}), db=db)
    _seed(db, [103], offsets=[2])
    with pytest.raises(AnalyticsError, match="snapshot changed"):
        evaluate_risk_model(request, db=db)


@pytest.mark.parametrize(
    "parameters",
    [
        {"risk_window": 1},
        {"risk_window": 2000},
        {"risk_window": "2"},
        {"tail_window": 59},
        {"tail_window": 2000},
        {"confidence": 0.99},
    ],
)
def test_risk_parameter_bounds(parameters):
    with pytest.raises(ValidationError):
        RiskParameters.model_validate(parameters)


@pytest.mark.parametrize("invalid", ["0", "-1", "NaN", "Infinity"])
def test_risk_rejects_nonpositive_or_nonfinite_levels(tmp_path, invalid):
    db = tmp_path / "risk.sqlite"
    _seed(db, [100, invalid])
    with pytest.raises(AnalyticsError, match="finite and positive"):
        _evaluate(db)


def test_risk_large_levels_and_single_observation_warmup(tmp_path):
    db = tmp_path / "risk.sqlite"
    _seed(db, ["1e80", "1.2e80", "9.6e79"])
    result = _evaluate(db, parameters={"risk_window": 2})
    assert Decimal(result.points[-1]["close"]) == Decimal("9.6e79")
    assert Decimal(result.points[-1]["current_drawdown"]) == Decimal("-0.2")
    assert Decimal(result.points[-1]["annualized_volatility"]) == pytest.approx(
        Decimal("20.16").sqrt()
    )
    (first,) = _evaluate(db, end=0).points
    assert first["historical_sample_count"] == 0
    assert first["historical_tail_status"] == "insufficient_history"
    assert first["annualized_volatility"] is None
    assert first["open_peak_date"] is None
    assert Decimal(first["maximum_drawdown"]) == 0


def test_risk_rejects_more_than_2000_observations(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "_START", date(2010, 1, 2))
    db = tmp_path / "risk.sqlite"
    _seed(db, [100] * 2001)
    with pytest.raises(AnalyticsError, match="2,000 observations"):
        _evaluate(db, end=2000)
