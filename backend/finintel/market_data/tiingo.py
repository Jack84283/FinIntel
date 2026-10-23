"""Tiingo raw daily prices adapter. Token is sent only in HTTP headers."""
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from finintel.market_data.models import DailyOHLCV, HistoricalBatch
from finintel.market_data.provider import (
    SYMBOL_PATTERN, ProviderError, ProviderPayloadInvalid, ProviderRateLimited,
    ProviderUnavailable,
)

BASE = "https://api.tiingo.com/tiingo/daily"
TERMS = "https://www.tiingo.com/about/terms"


def tiingo_configured() -> bool:
    return bool(os.environ.get("FININTEL_TIINGO_API_KEY", "").strip())


def parse_prices(data: bytes) -> list[DailyOHLCV]:
    if len(data) > 8 * 1024 * 1024:
        raise ProviderPayloadInvalid("Tiingo response exceeds safe size")
    try:
        payload = json.loads(data)
        if not isinstance(payload, list) or not 0 < len(payload) <= 12000:
            raise ValueError("Invalid Tiingo price history")
        bars = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Invalid Tiingo observation")
            bars.append(DailyOHLCV(
                session_date=date.fromisoformat(item["date"][:10]),
                open=Decimal(str(item["open"])), high=Decimal(str(item["high"])),
                low=Decimal(str(item["low"])), close=Decimal(str(item["close"])),
                volume=item["volume"],
            ))
        dates = [bar.session_date for bar in bars]
        if dates != sorted(set(dates)):
            raise ValueError("Duplicate or unordered Tiingo dates")
        return bars
    except (KeyError, ValueError, TypeError, ArithmeticError, ValidationError) as exc:
        raise ProviderPayloadInvalid("Tiingo price history failed validation") from exc


class TiingoDailyProvider:
    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._token = os.environ.get("FININTEL_TIINGO_API_KEY", "").strip()
        if not self._token:
            raise ProviderError("Configure FININTEL_TIINGO_API_KEY in the Python backend")
        self._client = client

    def fetch_daily(self, symbol: str, *, period: str = "10y") -> HistoricalBatch:
        ticker = symbol.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(ticker):
            raise ProviderError("Invalid stock/ETF ticker")
        end = datetime.now(UTC).date() - timedelta(days=1)
        if period == "max":
            start = date(1990, 1, 1)
        elif period in ("5y", "10y"):
            try:
                start = end.replace(year=end.year - int(period[:-1]))
            except ValueError:
                start = end.replace(year=end.year - int(period[:-1]), day=28)
        else:
            raise ProviderError("Unsupported Tiingo historical range")
        url = BASE + "/" + quote(ticker, safe="") + "/prices"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}
        if self._client is None:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0), follow_redirects=False) as client:
                bars = self._request(client, url, params)
        else:
            bars = self._request(self._client, url, params)
        return HistoricalBatch(
            provider="tiingo", symbol=ticker,
            source_url=url + "?startDate=" + params["startDate"] + "&endDate=" + params["endDate"],
            provider_terms_url=TERMS, observations=bars,
        )

    def _request(self, client: httpx.Client, url: str, params: dict[str, str]) -> list[DailyOHLCV]:
        try:
            response = client.get(
                url, params=params,
                headers={"Authorization": "Token " + self._token,
                         "Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise ProviderUnavailable("Could not reach Tiingo") from exc
        if response.status_code == 429:
            raise ProviderRateLimited("Tiingo request quota reached")
        if response.status_code in (401, 403):
            raise ProviderError("Tiingo authentication or access denied")
        if response.status_code == 404:
            raise ProviderError("Tiingo does not have this ticker")
        if response.status_code != 200:
            raise ProviderUnavailable("Tiingo history request unsuccessful")
        return parse_prices(response.content)
