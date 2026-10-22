"""Historical market-data contracts and provider adapters (PR #2).

No network request occurs when this package is imported.
"""

from finintel.market_data.models import DailyOHLCV, HistoricalBatch
from finintel.market_data.provider import (
    AlphaVantageDailyProvider,
    MarketDataProvider,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderUnavailable,
)

__all__ = [
    "AlphaVantageDailyProvider",
    "DailyOHLCV",
    "HistoricalBatch",
    "MarketDataProvider",
    "ProviderError",
    "ProviderPayloadInvalid",
    "ProviderRateLimited",
    "ProviderUnavailable",
]
