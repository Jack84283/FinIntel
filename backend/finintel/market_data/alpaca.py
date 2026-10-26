"""Bounded Alpaca historical daily stock/ETF bars adapter (SIP, raw).

Credentials are supplied only through backend environment variables and
headers. No provider response, key, or secret is logged or saved in SQLite.
"""
from finintel.settings.credentials import get_secret


import json
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from finintel.market_data.models import DailyOHLCV, HistoricalBatch
from finintel.market_data.provider import (
    SYMBOL_PATTERN,
    ProviderAccessDenied,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderSymbolUnavailable,
    ProviderUnavailable,
)

ALPACA_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_TERMS = "https://alpaca.markets/disclosures"
MAX_PAGES = 8
MAX_BARS = 12000
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# Basic permits 200 historical HTTP requests per minute; leave a safety margin.
DEFAULT_REQUESTS_PER_MINUTE = 180
_PACE_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0


def _pace_request() -> None:
    """Pace every Alpaca HTTP request, including subsequent pagination pages."""
    global _LAST_REQUEST_STARTED
    configured = os.environ.get(
        "FININTEL_ALPACA_REQUESTS_PER_MINUTE", str(DEFAULT_REQUESTS_PER_MINUTE)
    )
    try:
        per_minute = int(configured)
    except ValueError as exc:
        raise ProviderError("FININTEL_ALPACA_REQUESTS_PER_MINUTE must be an integer") from exc
    if not 1 <= per_minute <= 10000:
        raise ProviderError("Alpaca requests per minute must be between 1 and 10000")
    interval = 60.0 / per_minute
    with _PACE_LOCK:
        now = time.monotonic()
        delay = max(0.0, _LAST_REQUEST_STARTED + interval - now)
        if delay:
            time.sleep(delay)
        _LAST_REQUEST_STARTED = time.monotonic()


def alpaca_configured() -> bool:
    return bool(
        get_secret("alpaca_sip", "key_id").strip()
        and get_secret("alpaca_sip", "secret_key").strip()
    )


def _start_date(period: str, end: date) -> date:
    if period == "5y":
        return end.replace(year=end.year - 5)
    if period == "10y":
        return end.replace(year=end.year - 10)
    if period == "max":
        return date(2016, 1, 1)
    raise ProviderError("Alpaca history must be 5y, 10y, or max")


def _decode_page(content: bytes, symbol: str) -> tuple[list[DailyOHLCV], str | None]:
    if len(content) > MAX_RESPONSE_BYTES:
        raise ProviderPayloadInvalid("Alpaca page exceeds the safe response size")
    try:
        obj = json.loads(content)
        if not isinstance(obj, dict) or set(obj) - {"bars", "next_page_token", "symbol"}:
            raise ValueError("Unexpected Alpaca JSON fields")
        raw = obj["bars"]
        if not isinstance(raw, dict) or (raw and set(raw) != {symbol}):
            raise ValueError("Alpaca returned a different ticker")
        # Alpaca may legitimately return an empty bars object for an
        # unsupported symbol or one without history in the selected window.
        records = raw.get(symbol, [])
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError("Invalid Alpaca historical bars")
        token = obj.get("next_page_token")
        if token is not None and (not isinstance(token, str) or len(token) > 2048):
            raise ValueError("Invalid Alpaca next-page token")
        parsed: list[DailyOHLCV] = []
        for row in records:
            if not isinstance(row, dict):
                raise TypeError("Invalid Alpaca OHLCV record")
            timestamp = row["t"]
            if not isinstance(timestamp, str):
                raise TypeError("Invalid Alpaca bar timestamp")
            trading_date = datetime.fromisoformat(timestamp)
            if trading_date.tzinfo is None:
                raise ValueError("Alpaca bar requires an offset-aware timestamp")
            prices = []
            for field in ("o", "h", "l", "c"):
                value = row[field]
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    raise TypeError("Invalid Alpaca price type")
                prices.append(Decimal(str(value)))
            volume = row["v"]
            if isinstance(volume, bool) or not isinstance(volume, int):
                raise TypeError("Invalid Alpaca volume type")
            parsed.append(
                DailyOHLCV(
                    session_date=trading_date.astimezone(UTC).date(),
                    open=prices[0],
                    high=prices[1],
                    low=prices[2],
                    close=prices[3],
                    volume=volume,
                )
            )
        return parsed, token
    except (
        KeyError,
        ValueError,
        TypeError,
        InvalidOperation,
        OverflowError,
        ValidationError,
    ) as exc:
        raise ProviderPayloadInvalid("Alpaca historical bars failed validation") from exc


class AlpacaDailyProvider:
    """One explicitly requested bounded date interval, with page-token traversal."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._key = get_secret("alpaca_sip", "key_id").strip()
        self._secret = get_secret("alpaca_sip", "secret_key").strip()
        if not self._key or not self._secret:
            raise ProviderError(
                "Configure FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY"
            )
        self._client = client

    def fetch_daily(self, symbol: str, *, period: str = "10y") -> HistoricalBatch:
        ticker = symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(ticker):
            raise ProviderError("Invalid stock or ETF symbol")
        # A YYYY-MM-DD end is interpreted as midnight UTC. On free SIP access,
        # yesterday can fall inside the restricted 15-minute window shortly
        # after UTC midnight. Use the day before yesterday for a safe margin.
        end = datetime.now(UTC).date() - timedelta(days=2)
        try:
            start = _start_date(period, end)
        except ValueError:
            start = end.replace(year=end.year - (5 if period == "5y" else 10))
        if start > end:
            raise ProviderError("Alpaca history range is invalid")
        public_params = {
            "symbols": ticker,
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": "sip",
            "adjustment": "raw",
            "limit": "10000",
        }
        source_url = ALPACA_ENDPOINT + "?" + urlencode(public_params)
        if self._client:
            bars = self._fetch_pages(self._client, public_params, ticker)
        else:
            with httpx.Client(
                timeout=httpx.Timeout(25.0, connect=5.0),
                follow_redirects=False,
            ) as client:
                bars = self._fetch_pages(client, public_params, ticker)
        if not bars:
            raise ProviderSymbolUnavailable("Alpaca has no daily sessions for this instrument")
        return HistoricalBatch(
            provider="alpaca_sip",
            symbol=ticker,
            source_url=source_url,
            provider_terms_url=ALPACA_TERMS,
            observations=bars,
        )

    def _fetch_pages(
        self, client: httpx.Client, params: dict[str, str], symbol: str
    ) -> list[DailyOHLCV]:
        bars: list[DailyOHLCV] = []
        seen_pages: set[str] = set()
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            payload = dict(params)
            if page_token:
                payload["page_token"] = page_token
            try:
                _pace_request()
                response = client.get(
                    ALPACA_ENDPOINT,
                    params=payload,
                    headers={
                        "APCA-API-KEY-ID": self._key,
                        "APCA-API-SECRET-KEY": self._secret,
                    },
                )
            except httpx.RequestError as exc:
                raise ProviderUnavailable("Could not reach Alpaca historical data") from exc
            if response.status_code == 429:
                raise ProviderRateLimited("Alpaca quota reached; retry manually later")
            if response.status_code in (401, 403):
                raise ProviderAccessDenied(
                    "Alpaca credentials or historical SIP entitlement were rejected"
                )
            if response.status_code in (404, 422):
                raise ProviderSymbolUnavailable("Alpaca does not support this instrument")
            if response.status_code != 200:
                raise ProviderUnavailable("Alpaca daily-history request was unsuccessful")
            new_bars, next_token = _decode_page(response.content, symbol)
            if not new_bars and next_token is None and not bars:
                raise ProviderSymbolUnavailable("Alpaca has no bars for this instrument")
            for bar in new_bars:
                if bars and bar.session_date <= bars[-1].session_date:
                    raise ProviderPayloadInvalid(
                        "Alpaca returned overlapping or unordered daily dates"
                    )
                bars.append(bar)
                if len(bars) > MAX_BARS:
                    raise ProviderPayloadInvalid("Alpaca history exceeds the safe session limit")
            if next_token is None:
                return bars
            if next_token in seen_pages:
                raise ProviderPayloadInvalid("Alpaca returned a repeated page token")
            seen_pages.add(next_token)
            page_token = next_token
        raise ProviderPayloadInvalid("Alpaca history exceeds the page limit; no partial import")
