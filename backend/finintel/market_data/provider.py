"""Alpha Vantage daily historical equity adapter.

No live market request in tests. A user-provided key is required for live use.
The request URL contains the key; the public source URL and errors never do.
"""

import json
import re
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from finintel.settings.credentials import get_secret
from finintel.market_data.models import DailyOHLCV, HistoricalBatch

API_ENDPOINT = "https://www.alphavantage.co/query"
TERMS_URL = "https://www.alphavantage.co/terms_of_service/"
SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}\Z")
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class ProviderError(Exception):
    """Safe, credential-free error that may be shown to the user."""


class ProviderUnavailable(ProviderError):
    """Transient network/provider outage."""


class ProviderRateLimited(ProviderError):
    """Provider quota hit: do not retry automatically."""


class ProviderAccessDenied(ProviderError):
    """Provider rejected credentials or entitlement: stop requests immediately."""


class ProviderSymbolUnavailable(ProviderError):
    """Provider has no eligible price history for this instrument."""


class ProviderPayloadInvalid(ProviderError):
    """Response failed parsing or validation and must not be stored."""


class MarketDataProvider(Protocol):
    def fetch_daily(self, symbol: str, *, outputsize: str = "compact") -> HistoricalBatch: ...


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Detect silent duplicate dates/keys rather than accepting the last one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field from market-data provider")
        result[key] = value
    return result


def _daily_bar(day: str, values: Any) -> DailyOHLCV:
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Provider returned an invalid session date")
    if not isinstance(values, dict):
        raise TypeError("Provider returned invalid daily price fields")

    def price(field: str) -> Decimal:
        original = values[field]
        if not isinstance(original, str):
            raise TypeError("Provider price must be a decimal string")
        try:
            return Decimal(original)
        except InvalidOperation as exc:
            raise ValueError("Invalid provider decimal price") from exc

    raw_volume = values["5. volume"]
    if not isinstance(raw_volume, str) or not re.fullmatch(r"[0-9]+", raw_volume):
        raise ValueError("Provider volume must be a nonnegative integer string")

    return DailyOHLCV(
        session_date=date.fromisoformat(day),
        open=price("1. open"),
        high=price("2. high"),
        low=price("3. low"),
        close=price("4. close"),
        volume=int(raw_volume),
    )


def parse_alpha_vantage_daily(
    content: bytes,
    *,
    symbol: str,
    source_url: str,
) -> HistoricalBatch:
    """Strictly parse a successful TIME_SERIES_DAILY JSON payload."""
    try:
        payload = json.loads(content, object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError) as exc:
        raise ProviderPayloadInvalid("Provider returned malformed or ambiguous JSON") from exc

    if not isinstance(payload, dict):
        raise ProviderPayloadInvalid("Provider returned an unexpected JSON payload")

    # Alpha Vantage can return quota/error notifications as HTTP 200 JSON.
    message = payload.get("Note") or payload.get("Information")
    if message is not None:
        if isinstance(message, str) and any(
            word in message.lower() for word in ("limit", "frequency", "quota", "rate")
        ):
            raise ProviderRateLimited(
                "Alpha Vantage quota reached; try again after the provider limit resets"
            )
        raise ProviderError("Alpha Vantage did not grant access to this data request")

    if "Error Message" in payload:
        raise ProviderError("Alpha Vantage rejected the symbol or request")

    try:
        metadata = payload["Meta Data"]
        observations = payload["Time Series (Daily)"]
        if not isinstance(metadata, dict) or not isinstance(observations, dict):
            raise TypeError("Invalid metadata or series object")
        actual_symbol = metadata["2. Symbol"]
        if not isinstance(actual_symbol, str) or actual_symbol.upper() != symbol:
            raise ValueError("Returned instrument does not match the requested symbol")
        if not observations:
            raise ValueError("No daily observations returned")
        bars = [_daily_bar(day, values) for day, values in observations.items()]
        bars.sort(key=lambda point: point.session_date)
        last_refreshed = metadata.get("3. Last Refreshed")
        if last_refreshed is not None and not isinstance(last_refreshed, str):
            raise ValueError("Invalid provider refresh metadata")
        return HistoricalBatch(
            provider="alpha_vantage",
            symbol=symbol,
            source_url=source_url,
            provider_terms_url=TERMS_URL,
            provider_last_refreshed=last_refreshed,
            observations=bars,
        )
    except (KeyError, ValueError, ValidationError, TypeError) as exc:
        raise ProviderPayloadInvalid("Provider daily prices failed integrity checks") from exc


class AlphaVantageDailyProvider:
    """One symbol/request per call. No automatic retries on provider quotas.

    The default compact free API response includes at most 100 daily sessions.
    Callers must observe their account's daily quota across separate invocations.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = (api_key or get_secret("alpha_vantage", "api_key")).strip()
        if not self._api_key:
            raise ProviderError(
                "Set FININTEL_ALPHA_VANTAGE_API_KEY in the local environment "
                "before requesting provider data"
            )
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("max_attempts must be between 1 and 3")
        self._client = client
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def fetch_daily(self, symbol: str, *, outputsize: str = "compact") -> HistoricalBatch:
        instrument = symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(instrument):
            raise ProviderError("Invalid equity symbol format")
        if outputsize not in {"compact", "full"}:
            raise ProviderError("outputsize must be compact or full")

        public_params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": instrument,
            "outputsize": outputsize,
            "datatype": "json",
        }
        source_url = API_ENDPOINT + "?" + urlencode(public_params)
        request_params = {**public_params, "apikey": self._api_key}

        # An injected client belongs to the caller; otherwise own/close one per call.
        if self._client is not None:
            return self._request(self._client, request_params, instrument, source_url)
        with httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        ) as client:
            return self._request(client, request_params, instrument, source_url)

    def _request(
        self,
        client: httpx.Client,
        params: dict[str, str],
        symbol: str,
        source_url: str,
    ) -> HistoricalBatch:
        for attempt in range(self._max_attempts):
            try:
                response = client.get(API_ENDPOINT, params=params)
            except httpx.RequestError as exc:
                # Do not stringify httpx exceptions; some include the API key URL.
                if attempt + 1 < self._max_attempts:
                    self._sleeper(min(2.0, 0.25 * (2**attempt)))
                    continue
                raise ProviderUnavailable("Could not reach Alpha Vantage after retries") from exc

            if response.status_code == 429:
                raise ProviderRateLimited(
                    "Alpha Vantage rate limit reached; no automatic retry attempted"
                )
            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt + 1 < self._max_attempts:
                    self._sleeper(min(2.0, 0.25 * (2**attempt)))
                    continue
                raise ProviderUnavailable("Alpha Vantage unavailable after retries")
            if response.status_code != 200:
                raise ProviderError(
                    "Alpha Vantage rejected the request (HTTP " + str(response.status_code) + ")"
                )
            return parse_alpha_vantage_daily(response.content, symbol=symbol, source_url=source_url)
        raise ProviderUnavailable("Provider attempts exhausted")
