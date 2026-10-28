"""Bounded read-only provider connectivity diagnostics.

Never include credentials, request URLs, provider response bodies or response headers
in diagnostics: these may contain sensitive data.
"""

from time import monotonic

import httpx

from finintel.settings.credentials import FIELDS, get_secret

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
RESULT_MESSAGES = {
    "connected": "Authentication and selected service access succeeded.",
    "limited": "Credentials accepted but this service or subscription is not available.",
    "authentication_failed": "The provider rejected the saved credentials.",
    "rate_limited": "The provider has limited requests; try again later.",
    "unreachable": "Could not contact the provider within the timeout.",
    "invalid_response": "The provider returned an unexpected response.",
    "not_configured": "Save all required credentials before testing.",
}


def test_connection(provider: str, *, client: httpx.Client | None = None) -> dict:
    if provider not in FIELDS:
        raise ValueError("Unsupported provider")
    secrets = {field: get_secret(provider, field) for field in FIELDS[provider]}
    if not all(secrets.values()):
        return {
            "status": "not_configured",
            "message": RESULT_MESSAGES["not_configured"],
            "http_status": None,
            "elapsed_ms": 0,
        }

    if provider == "alpha_vantage":
        url = "https://www.alphavantage.co/query"
        kwargs = {
            "params": {"function": "SYMBOL_SEARCH", "keywords": "IBM", "apikey": secrets["api_key"]}
        }
    elif provider == "alpaca_sip":
        url = "https://data.alpaca.markets/v2/stocks/AAPL/bars"
        kwargs = {
            "params": {
                "timeframe": "1Day",
                "start": "2024-01-02T00:00:00Z",
                "end": "2024-01-04T00:00:00Z",
                "limit": 1,
                "feed": "sip",
            },
            "headers": {
                "APCA-API-KEY-ID": secrets["key_id"],
                "APCA-API-SECRET-KEY": secrets["secret_key"],
            },
        }
    elif provider == "tiingo":
        url = "https://api.tiingo.com/tiingo/daily/aapl"
        kwargs = {
            "headers": {
                "Authorization": "Token " + secrets["api_key"],
                "Accept": "application/json",
            }
        }
    else:
        url = "https://api.equibles.com/v1/indexes/sp-500"
        kwargs = {
            "params": {"limit": 1, "offset": 0},
            "headers": {
                "Authorization": "Bearer " + secrets["api_key"],
                "Accept": "application/json",
            },
        }

    start = monotonic()
    try:
        if client is None:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as session:
                response = session.get(url, **kwargs)
        else:
            response = client.get(url, **kwargs)
        http_status = response.status_code
        if http_status in (401, 403):
            status = "authentication_failed" if http_status == 401 else "limited"
        elif http_status == 429:
            status = "rate_limited"
        elif not 200 <= http_status < 300:
            status = "unreachable" if http_status >= 500 else "invalid_response"
        else:
            try:
                payload = response.json()
                if provider == "alpha_vantage":
                    if not isinstance(payload, dict):
                        status = "invalid_response"
                    elif "bestMatches" in payload and isinstance(payload["bestMatches"], list):
                        status = "connected"
                    elif "Note" in payload or (
                        "Information" in payload and "rate" in str(payload["Information"]).lower()
                    ):
                        status = "rate_limited"
                    elif "Error Message" in payload or "Information" in payload:
                        status = "limited"
                    else:
                        status = "invalid_response"
                elif provider == "alpaca_sip":
                    status = (
                        "connected"
                        if isinstance(payload, dict) and "bars" in payload
                        else "invalid_response"
                    )
                elif provider == "tiingo":
                    status = (
                        "connected"
                        if isinstance(payload, dict) and payload.get("ticker")
                        else "invalid_response"
                    )
                else:
                    status = (
                        "connected"
                        if isinstance(payload, dict) and isinstance(payload.get("data"), list)
                        else "invalid_response"
                    )
            except (ValueError, TypeError):
                status = "invalid_response"
    except httpx.RequestError:
        http_status = None
        status = "unreachable"
    elapsed = round((monotonic() - start) * 1000)
    return {
        "status": status,
        "message": RESULT_MESSAGES[status],
        "http_status": http_status,
        "elapsed_ms": elapsed,
    }
