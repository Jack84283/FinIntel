"""Equibles index constituents acquisition. Explicit user-triggered network access only."""

import json
import os
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from finintel.market_data.membership import INDEX_NAMES, load_membership

INDEX_SLUGS = {
    "SP500": "sp-500",
    "DJIA": "dow-jones-industrial-average",
    "NDX": "nasdaq-100",
    "RUT": "russell-2000",
}
PAGE_SIZE = 500


class EquiblesError(ValueError):
    """Safe diagnostic for provider lookup problems."""


def configured() -> bool:
    return bool(os.environ.get("FININTEL_EQUIBLES_API_KEY", "").strip())


def _fetch(code: str, offset: int) -> dict:
    key = os.environ.get("FININTEL_EQUIBLES_API_KEY", "").strip()
    if not key:
        raise EquiblesError("Configure FININTEL_EQUIBLES_API_KEY in the backend environment.")
    url = (
        "https://api.equibles.com/v1/indexes/"
        + INDEX_SLUGS[code]
        + "?"
        + urlencode({"limit": PAGE_SIZE, "offset": offset})
    )
    req = Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise EquiblesError("Equibles response exceeds the allowed size.")
        result = json.loads(raw)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise EquiblesError("Equibles rejected the API key or denied index access.") from exc
        if exc.code == 429:
            raise EquiblesError("Equibles rate limit reached; try again later.") from exc
        raise EquiblesError(f"Equibles returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EquiblesError("Equibles response is unavailable or invalid.") from exc
    if not isinstance(result, dict):
        raise EquiblesError("Equibles returned malformed index data.")
    return result


def acquire_index(code: str) -> dict:
    """Download every page and persist only after complete source validation."""
    if code not in INDEX_NAMES:
        raise EquiblesError("Unknown index.")
    symbols = []
    names = {}
    unlinked = []
    first = None
    offset = 0
    while True:
        page = _fetch(code, offset)
        meta = page.get("meta")
        data = page.get("data")
        if not isinstance(meta, dict) or not isinstance(data, list):
            raise EquiblesError("Equibles response missing data or pagination metadata.")
        if page.get("slug") != INDEX_SLUGS[code]:
            raise EquiblesError("Equibles returned the wrong index.")
        if first is None:
            first = page
            try:
                effective = date.fromisoformat(page["asOfDate"])
                if effective > date.today():
                    raise ValueError("future")
            except (KeyError, TypeError, ValueError) as exc:
                raise EquiblesError("Invalid Equibles snapshot date.") from exc
            expected = page.get("constituentCount")
            if not isinstance(expected, int) or not 1 <= expected <= 5000:
                raise EquiblesError("Invalid source constituent count.")
            if not isinstance(page.get("unlinkedCount"), int) or not 0 <= page["unlinkedCount"] <= expected:
                raise EquiblesError("Invalid source unlinked count.")
        if (
            page.get("asOfDate") != first["asOfDate"]
            or page.get("constituentCount") != first["constituentCount"]
            or meta.get("offset") != offset
            or meta.get("count") != len(data)
            or meta.get("total") != first["constituentCount"]
            or len(data) == 0
        ):
            raise EquiblesError("Equibles pages differ or returned incomplete data.")
        for row in data:
            if not isinstance(row, dict):
                raise EquiblesError("Encountered a malformed holding.")
            if row.get("isLinked") is not True or not row.get("ticker"):
                unlinked.append({"rank": row.get("rank"), "name": row.get("name")})
                continue
            ticker = row.get("ticker")
            if not isinstance(ticker, str):
                raise EquiblesError("Holding ticker must be a string.")
            symbols.append(ticker)
            names[ticker.strip().upper()] = row.get("name")
        offset += len(data)
        if offset > first["constituentCount"]:
            raise EquiblesError("Equibles returned more rows than declared.")
        if not meta.get("hasMore"):
            break
    if len(symbols) + len(unlinked) != first["constituentCount"]:
        raise EquiblesError("Equibles index download is incomplete.")
    if len(unlinked) != first["unlinkedCount"]:
        raise EquiblesError("Equibles unlinked count does not match downloaded rows.")
    if not symbols:
        raise EquiblesError("No linked tickers available to import.")
    source = "Equibles / ETF holdings proxy; " + INDEX_SLUGS[code] + "; " + first["asOfDate"]
    if unlinked:
        source += "; incomplete: " + str(len(unlinked)) + " unlinked holdings"
    count = load_membership(
        code,
        first["asOfDate"],
        source,
        symbols,
        expected_count=len(symbols),
        names=names,
    )
    return {
        "code": code,
        "snapshot_as_of": first["asOfDate"],
        "members": count,
        "source": source,
        "provenance": "fund_derived_proxy",
        "source_total": first["constituentCount"],
        "unlinked_count": len(unlinked),
        "complete": len(unlinked) == 0,
        "unlinked_holdings": unlinked[:20],
    }
