"""One explicit provider request -> validated snapshot -> atomic SQLite ingestion.

The temporary private staging snapshot allows reuse of PR #4's fully validated,
transactional importer. The JSON is removed after import: SQLite is the durable
store, and the CLI remains available for users who want permanent JSON exports.
"""

import os
import tempfile
from pathlib import Path

from finintel.market_data.__main__ import write_snapshot
from finintel.market_data.alpaca import AlpacaDailyProvider, alpaca_configured
from finintel.market_data.provider import (
    SYMBOL_PATTERN,
    AlphaVantageDailyProvider,
    ProviderError,
)
from finintel.storage.db import import_snapshot


def key_configured() -> bool:
    """Expose only whether a provider key is present, never its actual value."""
    return bool(os.environ.get("FININTEL_ALPHA_VANTAGE_API_KEY", "").strip())


def acquire_and_store(
    symbol: str,
    *,
    outputsize: str = "compact",
    provider: str = "alpha_vantage",
    period: str = "10y",
) -> dict[str, object]:
    """Fetch one symbol on user request, never on page load or a timer."""
    ticker = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(ticker):
        raise ProviderError("Enter a valid equity ticker symbol")
    if outputsize not in ("compact", "full"):
        raise ProviderError("Output size must be compact or full")
    if provider == "alpha_vantage":
        if not key_configured():
            raise ProviderError("Configure FININTEL_ALPHA_VANTAGE_API_KEY and restart the backend")
        batch = AlphaVantageDailyProvider().fetch_daily(ticker, outputsize=outputsize)
    elif provider == "alpaca_sip":
        if period not in ("5y", "10y", "max"):
            raise ProviderError("Invalid Alpaca historical range")
        if not alpaca_configured():
            raise ProviderError(
                "Configure FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY"
            )
        batch = AlpacaDailyProvider().fetch_daily(ticker, period=period)
    else:
        raise ProviderError("Unsupported price history source")
    # The provider returns a fully validated HistoricalBatch or raises an error.
    with tempfile.TemporaryDirectory(prefix="finintel-acquisition-") as temporary:
        snapshot = Path(temporary) / (ticker + ".json")
        write_snapshot(snapshot, batch)
        imported = import_snapshot(snapshot)

    return {
        "status": "stored",
        "symbol": ticker,
        "provider": batch.provider,
        "frequency": batch.frequency,
        "price_adjustment": batch.price_adjustment,
        "first_session": batch.first_session.isoformat(),
        "last_session": batch.last_session.isoformat(),
        "retrieved_at": batch.retrieved_at.isoformat(),
        "observations": imported["observations"],
        "inserted": imported["inserted"],
        "updated": imported["updated"],
        "unchanged": imported["unchanged"],
        "stale": imported["stale"],
        "import_id": imported["import_id"],
        "not_live_quotes": True,
    }
