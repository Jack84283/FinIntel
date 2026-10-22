"""Offline provider contracts. Fixture values are intentionally synthetic."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from finintel.market_data import (
    AlphaVantageDailyProvider,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderUnavailable,
)
from finintel.market_data.provider import parse_alpha_vantage_daily

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
RAW = FIXTURE.read_bytes()
SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM"


def mock_provider(handler, *, max_attempts=3, sleeper=lambda _: None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AlphaVantageDailyProvider(
        api_key="TEST_KEY_NOT_REAL", client=client, max_attempts=max_attempts, sleeper=sleeper
    )


def test_parse_fixture_is_historical_raw_and_sorted():
    batch = parse_alpha_vantage_daily(RAW, symbol="IBM", source_url=SOURCE)
    assert batch.provider == "alpha_vantage"
    assert batch.symbol == "IBM"
    assert batch.price_adjustment == "raw_unadjusted"
    assert batch.data_kind == "historical_eod"
    assert len(batch.observations) == 3
    assert batch.first_session.isoformat() == "2026-10-16"
    assert batch.last_session.isoformat() == "2026-10-20"
    assert batch.observations[-1].close == Decimal("104.10")
    assert batch.observations[0].volume == 450000
    assert batch.retrieved_at.tzinfo is not None


def test_request_parameters_and_credentials_never_appear_in_output():
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, content=RAW)

    batch = mock_provider(handler).fetch_daily(" ibm ")
    assert len(called) == 1
    assert called[0].url.scheme == "https"
    assert called[0].url.params["function"] == "TIME_SERIES_DAILY"
    assert called[0].url.params["outputsize"] == "compact"
    assert called[0].url.params["apikey"] == "TEST_KEY_NOT_REAL"
    assert "apikey" not in batch.source_url
    assert "TEST_KEY_NOT_REAL" not in batch.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["Time Series (Daily)"]["2026-10-20"].update({"2. high": "99"}),
        lambda p: p["Time Series (Daily)"]["2026-10-20"].update({"1. open": "NaN"}),
        lambda p: p["Time Series (Daily)"]["2026-10-20"].update({"5. volume": "-1"}),
        lambda p: p["Time Series (Daily)"]["2026-10-20"].update({"4. close": "1e99999"}),
        lambda p: p["Meta Data"].update({"2. Symbol": "MSFT"}),
    ],
)
def test_invalid_market_data_is_rejected(mutation):
    payload = json.loads(RAW)
    mutation(payload)
    with pytest.raises(ProviderPayloadInvalid):
        parse_alpha_vantage_daily(json.dumps(payload).encode(), symbol="IBM", source_url=SOURCE)


def test_bad_date_is_rejected():
    payload = json.loads(RAW)
    payload["Time Series (Daily)"]["2026-13-99"] = payload["Time Series (Daily)"].pop("2026-10-20")
    with pytest.raises(ProviderPayloadInvalid):
        parse_alpha_vantage_daily(json.dumps(payload).encode(), symbol="IBM", source_url=SOURCE)


def test_duplicate_json_key_is_rejected():
    # Avoid Python dict normalization when building the malicious response.
    corrupt = RAW.replace(b'"2026-10-20": {', b'"2026-10-20": {}, "2026-10-20": {', 1)
    with pytest.raises(ProviderPayloadInvalid):
        parse_alpha_vantage_daily(corrupt, symbol="IBM", source_url=SOURCE)


@pytest.mark.parametrize(
    "body",
    [
        {"Note": "Your API call frequency is limited"},
        {"Information": "The rate limit is 25 requests per day"},
    ],
)
def test_provider_quota_response_does_not_retry(body):
    call_count = 0

    def handler(_):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderRateLimited):
        mock_provider(handler).fetch_daily("IBM")
    assert call_count == 1


def test_http_429_does_not_retry():
    call_count = 0

    def handler(_):
        nonlocal call_count
        call_count += 1
        return httpx.Response(429)

    with pytest.raises(ProviderRateLimited):
        mock_provider(handler).fetch_daily("IBM")
    assert call_count == 1


def test_server_failure_retries_with_bounded_backoff():
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=RAW)

    batch = mock_provider(handler, sleeper=delays.append).fetch_daily("IBM")
    assert batch.symbol == "IBM"
    assert len(calls) == 3
    assert delays == [0.25, 0.5]


def test_transport_failure_is_credential_safe():
    def handler(request):
        raise httpx.ConnectError("secret: " + str(request.url), request=request)

    with pytest.raises(ProviderUnavailable) as caught:
        mock_provider(handler, max_attempts=2).fetch_daily("IBM")
    assert "TEST_KEY_NOT_REAL" not in str(caught.value)


def test_provider_auth_and_symbol_errors_are_safe():
    with pytest.raises(ProviderError, match="Invalid equity symbol"):
        mock_provider(lambda _: httpx.Response(200, content=RAW)).fetch_daily("../IBM")

    with pytest.raises(ProviderError, match="rejected"):
        mock_provider(lambda _: httpx.Response(200, json={"Error Message": "bad"})).fetch_daily(
            "IBM"
        )


def test_missing_key_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("FININTEL_ALPHA_VANTAGE_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="FININTEL_ALPHA_VANTAGE_API_KEY"):
        AlphaVantageDailyProvider()


def test_bad_body_and_empty_series_are_rejected():
    with pytest.raises(ProviderPayloadInvalid):
        parse_alpha_vantage_daily(b"<html>maintenance</html>", symbol="IBM", source_url=SOURCE)
    payload = json.loads(RAW)
    payload["Time Series (Daily)"] = {}
    with pytest.raises(ProviderPayloadInvalid):
        parse_alpha_vantage_daily(json.dumps(payload).encode(), symbol="IBM", source_url=SOURCE)
