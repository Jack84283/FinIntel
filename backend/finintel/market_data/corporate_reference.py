"""Explicit, read-only Tiingo split/dividend reference verification.

Never initiated by an ordinary Data Validation scan. Reference requests require
an operator action and a locally configured provider token.
"""

import os
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import httpx

from finintel.market_data.provider import SYMBOL_PATTERN, ProviderError


def verify_events(
    db: Path,
    symbol: str,
    *,
    client: httpx.Client | None = None,
) -> dict:
    ticker = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(ticker):
        raise ValueError("Invalid equity ticker symbol.")
    token = os.environ.get("FININTEL_TIINGO_API_KEY", "").strip()
    if not token:
        raise ProviderError("Configure FININTEL_TIINGO_API_KEY to verify corporate actions.")
    if not db.is_file():
        raise FileNotFoundError(db)
    url = "https://api.tiingo.com/tiingo/daily/" + quote(ticker, safe="") + "/prices"
    own_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))
    try:
        try:
            response = http_client.get(
                url,
                params={"startDate": "2010-01-01", "endDate": date.today().isoformat()},
                headers={"Authorization": "Token " + token, "Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise ProviderError("Tiingo reference provider is unavailable.") from exc
        if response.status_code != 200:
            raise ProviderError("Tiingo corporate-action reference request was unsuccessful.")
        if len(response.content) > 8 * 1024 * 1024:
            raise ProviderError("Tiingo corporate-action response exceeded safe size.")
        payload = response.json()
        if not isinstance(payload, list) or len(payload) > 12000:
            raise ProviderError("Tiingo returned invalid corporate-action reference data.")
        events = []
        for row in payload:
            if not isinstance(row, dict):
                raise ProviderError("Invalid corporate-action event.")
            day = date.fromisoformat(str(row["date"])[:10]).isoformat()
            try:
                split = Decimal(str(row.get("splitFactor", "1")))
                dividend = Decimal(str(row.get("divCash", "0")))
            except (InvalidOperation, TypeError) as exc:
                raise ProviderError("Invalid corporate-action decimal value.") from exc
            if not split.is_finite() or split <= 0 or not dividend.is_finite() or dividend < 0:
                raise ProviderError("Invalid corporate-action factor or dividend.")
            if split != 1 or dividend > 0:
                events.append(
                    {
                        "date": day,
                        "split_factor": str(split),
                        "cash_dividend": str(dividend),
                    }
                )
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT d.session_date, d.close, s.provider, s.adjustment "
                "FROM daily_prices d JOIN series s ON s.id=d.series_id "
                "WHERE s.symbol=? AND s.provider='alpaca_sip' "
                "ORDER BY d.session_date",
                (ticker,),
            )
            closes = {row["session_date"]: row["close"] for row in rows}
        finally:
            conn.close()
        for event in events:
            if Decimal(event["split_factor"]) == 1:
                event["check"] = "dividend_reference_only"
                continue
            earlier = [day for day in closes if day < event["date"]]
            if not earlier or event["date"] not in closes:
                event["check"] = "insufficient_local_price_history"
                continue
            previous = max(earlier)
            before, after = Decimal(closes[previous]), Decimal(closes[event["date"]])
            if before <= 0 or after <= 0:
                event["check"] = "invalid_local_close"
                continue
            actual = after / before
            expected = Decimal(1) / Decimal(event["split_factor"])
            event["observed_close_ratio"] = str(round(actual, 5))
            event["expected_split_ratio"] = str(round(expected, 5))
            event["check"] = (
                "split_ratio_consistent"
                if abs(actual - expected) / expected <= Decimal("0.25")
                else "manual_review"
            )
        return {
            "symbol": ticker,
            "source": "Tiingo daily event fields",
            "events": events,
            "event_count": len(events),
            "read_only": True,
            "limitations": [
                "Split price ratios are screening checks, not conclusive evidence of adjustment errors.",
                "Dividends are reference events; adjustment verification needs compatible adjusted bars.",
                "Local pre-listing, halt and provider differences may affect comparisons.",
            ],
        }
    finally:
        if own_client:
            http_client.close()
