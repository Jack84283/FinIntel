"""Typed historical price data. Prices are decimals, never binary floats."""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DailyOHLCV(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0, strict=True)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def positive_finite_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("OHLC prices must be finite and positive")
        return value

    @model_validator(mode="after")
    def consistent_candle(self) -> "DailyOHLCV":
        if self.high < self.low:
            raise ValueError("high must not be below low")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must lie between low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must lie between low and high")
        return self


class HistoricalBatch(BaseModel):
    """One validated provider response, before PR #3's SQLite import layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["alpha_vantage"]
    symbol: str
    frequency: Literal["1d"] = "1d"
    price_adjustment: Literal["raw_unadjusted"] = "raw_unadjusted"
    data_kind: Literal["historical_eod"] = "historical_eod"
    source_url: str
    provider_terms_url: str
    provider_last_refreshed: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    observations: list[DailyOHLCV] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_dates(self) -> "HistoricalBatch":
        dates = [point.session_date for point in self.observations]
        if dates != sorted(set(dates)):
            raise ValueError("observations must have unique, ascending dates")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must have a timezone")
        return self

    @property
    def first_session(self) -> date:
        return self.observations[0].session_date

    @property
    def last_session(self) -> date:
        return self.observations[-1].session_date
