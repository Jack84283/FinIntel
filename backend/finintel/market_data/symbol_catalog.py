"""Persistent instrument starter catalog and validated Alpha Vantage listing ingestion.

Only explicit user actions contact the listing-status provider. The starter
catalog is deliberately small and is not a complete or real-time listing.
"""
from finintel.settings.credentials import get_secret


import csv
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from finintel.market_data.provider import (
    API_ENDPOINT,
    SYMBOL_PATTERN,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderUnavailable,
)

# Fixed built-in offline examples; source metadata must not imply live status.
# Names describe the underlying asset; no price observations are included.
_STARTER: tuple[tuple[str, str, str], ...] = (
    ("AAPL", "Apple", "Stock"),
    ("ABBV", "AbbVie", "Stock"),
    ("ADBE", "Adobe", "Stock"),
    ("AMD", "Advanced Micro Devices", "Stock"),
    ("AMZN", "Amazon", "Stock"),
    ("AVGO", "Broadcom", "Stock"),
    ("BAC", "Bank of America", "Stock"),
    ("BRK.B", "Berkshire Hathaway Class B", "Stock"),
    ("COST", "Costco Wholesale", "Stock"),
    ("CRM", "Salesforce", "Stock"),
    ("DIS", "Walt Disney", "Stock"),
    ("GOOG", "Alphabet Class C", "Stock"),
    ("GOOGL", "Alphabet Class A", "Stock"),
    ("HD", "Home Depot", "Stock"),
    ("IBM", "IBM", "Stock"),
    ("INTC", "Intel", "Stock"),
    ("JNJ", "Johnson & Johnson", "Stock"),
    ("JPM", "JPMorgan Chase", "Stock"),
    ("KO", "Coca-Cola", "Stock"),
    ("LLY", "Eli Lilly", "Stock"),
    ("MA", "Mastercard", "Stock"),
    ("META", "Meta Platforms", "Stock"),
    ("MSFT", "Microsoft", "Stock"),
    ("NFLX", "Netflix", "Stock"),
    ("NVDA", "NVIDIA", "Stock"),
    ("ORCL", "Oracle", "Stock"),
    ("PANW", "Palo Alto Networks", "Stock"),
    ("PLTR", "Palantir Technologies", "Stock"),
    ("PYPL", "PayPal", "Stock"),
    ("QCOM", "Qualcomm", "Stock"),
    ("SHOP", "Shopify", "Stock"),
    ("TSLA", "Tesla", "Stock"),
    ("TXN", "Texas Instruments", "Stock"),
    ("UNH", "UnitedHealth Group", "Stock"),
    ("V", "Visa", "Stock"),
    ("WMT", "Walmart", "Stock"),
    ("ARKK", "ARK Innovation ETF", "ETF"),
    ("BND", "Vanguard Total Bond Market ETF", "ETF"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF Trust", "ETF"),
    ("EEM", "iShares MSCI Emerging Markets ETF", "ETF"),
    ("EFA", "iShares MSCI EAFE ETF", "ETF"),
    ("GLD", "SPDR Gold Shares", "ETF"),
    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF", "ETF"),
    ("IEMG", "iShares Core MSCI Emerging Markets ETF", "ETF"),
    ("IWM", "iShares Russell 2000 ETF", "ETF"),
    ("IVV", "iShares Core S&P 500 ETF", "ETF"),
    ("LQD", "iShares iBoxx Investment Grade Corporate Bond ETF", "ETF"),
    ("QQQ", "Invesco QQQ Trust", "ETF"),
    ("SCHD", "Schwab US Dividend Equity ETF", "ETF"),
    ("SLV", "iShares Silver Trust", "ETF"),
    ("SPY", "SPDR S&P 500 ETF Trust", "ETF"),
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "ETF"),
    ("USO", "United States Oil Fund", "ETF"),
    ("VEA", "Vanguard FTSE Developed Markets ETF", "ETF"),
    ("VGT", "Vanguard Information Technology ETF", "ETF"),
    ("VIG", "Vanguard Dividend Appreciation ETF", "ETF"),
    ("VTI", "Vanguard Total Stock Market ETF", "ETF"),
    ("VOO", "Vanguard S&P 500 ETF", "ETF"),
    ("VWO", "Vanguard FTSE Emerging Markets ETF", "ETF"),
    ("XLE", "Energy Select Sector SPDR Fund", "ETF"),
    ("XLF", "Financial Select Sector SPDR Fund", "ETF"),
    ("XLK", "Technology Select Sector SPDR Fund", "ETF"),
    ("XLV", "Health Care Select Sector SPDR Fund", "ETF"),
    # Index identifiers follow common ^-prefixed reference symbols, NOT AV daily tickers.
    ("^GSPC", "S&P 500 Index", "Index"),
    ("^DJI", "Dow Jones Industrial Average", "Index"),
    ("^IXIC", "NASDAQ Composite Index", "Index"),
    ("^RUT", "Russell 2000 Index", "Index"),
    ("^NDX", "NASDAQ 100 Index", "Index"),
    ("^VIX", "CBOE Volatility Index", "Index"),
    ("FXAIX", "Fidelity 500 Index Fund", "Mutual Fund"),
    ("VFIAX", "Vanguard 500 Index Fund Admiral Shares", "Mutual Fund"),
    ("VTSAX", "Vanguard Total Stock Market Index Fund Admiral Shares", "Mutual Fund"),
    ("SWPPX", "Schwab S&P 500 Index Fund", "Mutual Fund"),
)

STARTER_SYMBOLS = tuple(
    {"symbol": symbol, "name": name, "asset_type": kind, "exchange": ""}
    for symbol, name, kind in _STARTER
)
ASSET_TYPES = ("Stock", "ETF", "Index", "Mutual Fund")
PRICE_FETCH_SUPPORTED = ("Stock", "ETF")

MAX_LISTING_BYTES = 16 * 1024 * 1024
MAX_LISTING_ROWS = 100_000


def search_starter(query: str, asset_type: str | None, limit: int) -> list[dict[str, str]]:
    terms = query.strip().upper()
    matched = [
        item
        for item in STARTER_SYMBOLS
        if (not asset_type or item["asset_type"] == asset_type)
        and (terms in item["symbol"] or terms in item["name"].upper())
    ]
    matched.sort(key=lambda item: (not item["symbol"].startswith(terms), item["symbol"]))
    return matched[:limit]


def parse_listing_csv(payload: bytes) -> list[dict[str, str]]:
    """Validate full active-listing response before any SQLite modification."""
    if not payload or len(payload) > MAX_LISTING_BYTES:
        raise ProviderPayloadInvalid("Listing feed is empty or exceeds the import size limit")
    if payload.lstrip().startswith(b"{"):
        try:
            response = json.loads(payload)
        except ValueError as exc:
            raise ProviderPayloadInvalid("Invalid listing provider response") from exc
        if any(field in response for field in ("Note", "Information")):
            raise ProviderRateLimited(
                "Alpha Vantage listing request is unavailable or rate limited; try again later"
            )
        raise ProviderError("Alpha Vantage did not return its stock and ETF listing")
    try:
        content = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
        fields = set(reader.fieldnames or [])
        required = {"symbol", "name", "exchange", "assetType", "status"}
        if not required <= fields:
            raise ProviderPayloadInvalid("Listing feed has unexpected CSV fields")
        result: dict[str, dict[str, str]] = {}
        kinds: set[str] = set()
        for count, row in enumerate(reader, start=1):
            if count > MAX_LISTING_ROWS:
                raise ProviderPayloadInvalid("Listing feed contains too many records")
            if not isinstance(row, dict) or None in row or row["status"] is None:
                raise ProviderPayloadInvalid("Listing feed contains invalid records")
            if row["status"].strip().lower() != "active":
                continue
            kind = row["assetType"].strip()
            if kind not in ("Stock", "ETF"):
                continue
            ticker = row["symbol"].strip().upper()
            # The current daily-price API supports simple ticker identifiers.
            if not SYMBOL_PATTERN.fullmatch(ticker):
                continue
            name = row["name"].strip()
            exchange = row["exchange"].strip()
            if (
                not name
                or len(name) > 200
                or len(exchange) > 80
                or any(ord(char) < 32 for char in name + exchange)
            ):
                # A small number of dirty provider rows must not invalidate a
                # complete listing. Exclude unusable records; never persist them.
                continue
            candidate = {
                "symbol": ticker,
                "name": name,
                "asset_type": kind,
                "exchange": exchange,
            }
            if ticker in result and result[ticker] != candidate:
                raise ProviderPayloadInvalid("Conflicting duplicate ticker in listing feed")
            result[ticker] = candidate
            kinds.add(kind)
        if not result or kinds != {"Stock", "ETF"}:
            raise ProviderPayloadInvalid("Listing feed must contain active stocks and ETFs")
        return list(result.values())
    except (UnicodeError, csv.Error, AttributeError, TypeError) as exc:
        raise ProviderPayloadInvalid("Invalid stock and ETF listing CSV") from exc


def fetch_active_listings(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """One user-authorized provider request. Never auto-refresh or log the key."""
    key = get_secret("alpha_vantage", "api_key").strip()
    if not key:
        raise ProviderError("Configure FININTEL_ALPHA_VANTAGE_API_KEY in the Python backend")
    params = {"function": "LISTING_STATUS", "state": "active", "apikey": key}

    def request(http: httpx.Client) -> list[dict[str, str]]:
        try:
            response = http.get(API_ENDPOINT, params=params)
        except httpx.RequestError as exc:
            raise ProviderUnavailable("Could not reach the Alpha Vantage listing service") from exc
        if response.status_code == 429:
            raise ProviderRateLimited("Alpha Vantage listing quota reached; try again later")
        if response.status_code != 200:
            raise ProviderError("Alpha Vantage stock/ETF listing request was rejected")
        return parse_listing_csv(response.content)

    if client is not None:
        return request(client)
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
    ) as http:
        return request(http)


def synchronized_rows(
    records: Sequence[dict[str, str]],
) -> tuple[list[tuple[str, str, str, str, str, str]], str]:
    """Attach source/time metadata after complete feed validation."""
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    return (
        [
            (
                item["symbol"],
                item["name"],
                item["asset_type"],
                item["exchange"],
                "alpha_vantage",
                timestamp,
            )
            for item in records
        ],
        timestamp,
    )
