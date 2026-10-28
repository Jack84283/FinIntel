"""Offline Alpaca error classification for large, unattended historical backfills."""

from datetime import date

import httpx
import pytest

from finintel.market_data import alpaca
from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderPayloadInvalid,
    ProviderSymbolUnavailable,
)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("FININTEL_ALPACA_API_KEY_ID", "TEST_KEY")
    monkeypatch.setenv("FININTEL_ALPACA_API_SECRET_KEY", "TEST_SECRET")
    monkeypatch.setattr(alpaca, "_pace_request", lambda: None)


@pytest.mark.parametrize("code", [401, 403])
def test_alpaca_auth_and_entitlement_rejection_is_fatal(credentials, code):
    def respond(request):
        assert request.headers["APCA-API-KEY-ID"] == "TEST_KEY"
        return httpx.Response(code, json={"message": "not authorized"})

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(ProviderAccessDenied),
    ):
        alpaca.AlpacaDailyProvider(client=client).fetch_daily("AAPL")


@pytest.mark.parametrize("code", [404, 422])
def test_alpaca_unsupported_symbols_are_isolated(credentials, code):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code))) as client,
        pytest.raises(ProviderSymbolUnavailable),
    ):
        alpaca.AlpacaDailyProvider(client=client).fetch_daily("AAC-WS")


@pytest.mark.parametrize(
    "body",
    [
        {"bars": {}, "next_page_token": None},
        {"bars": {"AAC-U": []}, "next_page_token": None},
    ],
)
def test_alpaca_no_bars_is_symbol_unavailable(credentials, body):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        ) as client,
        pytest.raises(ProviderSymbolUnavailable),
    ):
        alpaca.AlpacaDailyProvider(client=client).fetch_daily("AAC-U")


def test_alpaca_wrong_symbol_remains_invalid_payload(credentials):
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"bars": {"MSFT": []}})
            )
        ) as client,
        pytest.raises(ProviderPayloadInvalid),
    ):
        alpaca.AlpacaDailyProvider(client=client).fetch_daily("AAPL")


@pytest.mark.parametrize(
    ("period", "expected"),
    [("5y", date(2023, 2, 28)), ("10y", date(2018, 2, 28))],
)
def test_alpaca_leap_day_history_start_is_safe(period, expected):
    assert alpaca._start_date(period, date(2028, 2, 29)) == expected
