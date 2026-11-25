"""Source-pinned, observed-session Market comparisons on one read transaction."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finintel.market_data.analytics import AnalyticsError, validated_observations
from finintel.market_data.analytics_store import (
    SeriesLimitExceeded,
    SeriesUnavailable,
    _quality_status,
    _read_series,
)
from finintel.storage.db import _read_connection, database_path

PATH = "/api/v1/market-data/comparison"
MAX_DAYS = 3660
CONTRACT = "market-comparison-v1"
REVISION = "market-comparison-decimal-v1"
INDEXES = {"SP500", "DJIA", "NASDAQCOM", "NDX"}
EQUITY_PROVIDERS = {"alpha_vantage", "alpaca_sip", "tiingo"}
Warning = Literal["price_level_only", "mixed_semantics", "quality_unverified", "coverage_gaps"]


class ComparisonError(ValueError):
    """Safe, transport-facing refusal code."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Contract):
    kind: Literal["index", "equity"]
    code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")]
    provider: Literal["fred", "alpha_vantage", "alpaca_sip", "tiingo"]

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.code}"


class ComparisonRequest(Contract):
    contract_version: Literal["market-comparison-v1"]
    sources: Annotated[list[Source], Field(min_length=1, max_length=4)]
    start: date
    end: date

    @model_validator(mode="after")
    def validate_selection(self) -> ComparisonRequest:
        if len({source.key for source in self.sources}) != len(self.sources):
            raise ValueError("duplicate_source")
        if self.start > self.end or self.end > datetime.now(UTC).date():
            raise ValueError("invalid_window")
        return self


class Values(Contract):
    close: str
    normalized_100: str
    cumulative_return_pct: str


class Point(Contract):
    session_date: date
    values: list[Values | None]


class Evidence(Contract):
    key: str
    identity: Source
    adjustment: Literal["price_index", "raw_unadjusted"]
    return_semantics: Literal["price_index", "raw_unadjusted_equity_price_usd_per_share"]
    frequency: Literal["daily", "1d"]
    units: Literal["index_points", "usd_per_share"]
    snapshot_sha256: str
    first_date: date
    latest_date: date
    baseline_close: str
    common_end_close: str
    common_end_return_pct: str
    latest_close: str
    observations: int
    missing_sessions: int
    nonshared_sessions: int


class ComparisonResponse(Contract):
    contract_version: Literal["market-comparison-v1"] = CONTRACT
    calculation_revision: Literal["market-comparison-decimal-v1"] = REVISION
    request: ComparisonRequest
    baseline_date: date
    last_common_date: date
    shared_sessions: int
    sources: list[Evidence]
    points: list[Point]
    fingerprint_sha256: str
    output_sha256: str
    warnings: list[Warning]
    read_only: Literal[True] = True
    no_network: Literal[True] = True
    no_synthetic_sessions: Literal[True] = True


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation_map(rows: list[tuple[str, str]]) -> dict[date, tuple[Decimal, str]]:
    if any(not isinstance(raw, str) or len(raw) > 48 for _, raw in rows):
        raise ComparisonError("invalid_close")
    try:
        observations = validated_observations(rows)
    except AnalyticsError as exc:
        raise ComparisonError("invalid_close") from exc
    result = {}
    for observation in observations:
        close = observation.close
        # Bound expansion of scientific notation before allocating plain text.
        if not -46 <= close.adjusted() < 48 or close.as_tuple().exponent < -46:
            raise ComparisonError("invalid_close")
        text = format(close, "f")
        if len(text) > 48:
            raise ComparisonError("invalid_close")
        result[observation.session_date] = close, text
    return result


def _rounded(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    if not rounded:
        return "0"
    return format(rounded, "f").rstrip("0").rstrip(".")


def _values(row: tuple[Decimal, str], baseline: Decimal) -> Values:
    ratio = row[0] / baseline
    return Values(
        close=row[1],
        normalized_100=_rounded(ratio * 100),
        cumulative_return_pct=_rounded((ratio - 1) * 100),
    )


def run_market_comparison(
    request: ComparisonRequest, *, db: Path | None = None
) -> ComparisonResponse:
    if (request.end - request.start).days > MAX_DAYS:
        raise ComparisonError("resource_limit")
    for source in request.sources:
        if source.kind == "index" and source.code == "VIX":
            raise ComparisonError("unsupported_vix")
        if source.kind == "index" and (source.code not in INDEXES or source.provider != "fred"):
            raise ComparisonError("invalid_source")
        if source.kind == "equity" and source.provider not in EQUITY_PROVIDERS:
            raise ComparisonError("invalid_source")
    connection = _read_connection(db if db is not None else database_path())
    if connection is None:
        raise ComparisonError("missing_history")

    maps = []
    identities = []
    hashes = []
    with closing(connection) as conn:
        for source in request.sources:
            try:
                identity, rows, provenance = _read_series(
                    conn,
                    kind=source.kind,
                    code=source.code,
                    provider=source.provider,
                    start=request.start,
                    end=request.end,
                    max_observations=MAX_DAYS + 1,
                )
            except SeriesLimitExceeded as exc:
                raise ComparisonError("resource_limit") from exc
            except SeriesUnavailable as exc:
                if (
                    source.kind == "equity"
                    and conn.execute(
                        "SELECT 1 FROM series WHERE symbol=? AND provider=? LIMIT 1",
                        (source.code, source.provider),
                    ).fetchone()
                    is not None
                ):
                    raise ComparisonError("incompatible_source") from exc
                raise ComparisonError("missing_history") from exc
            if not rows:
                raise ComparisonError("missing_history")
            if identity.return_semantics not in {
                "price_index",
                "raw_unadjusted_equity_price_usd_per_share",
            }:
                raise ComparisonError("incompatible_source")
            maps.append(_observation_map(rows))
            hashes.append(provenance["snapshot_sha256"])
            identities.append(identity)
        quality = _quality_status(conn, [source.code for source in request.sources])

    shared = sorted(set.intersection(*(set(rows) for rows in maps)))
    if len(shared) < 2:
        raise ComparisonError("insufficient_overlap")
    baseline, common_end = shared[0], shared[-1]
    days = sorted(day for day in set.union(*(set(rows) for rows in maps)) if day >= baseline)
    warnings: list[Warning] = ["price_level_only"]
    if len({source.return_semantics for source in identities}) > 1:
        warnings.append("mixed_semantics")
    if not quality["quality_verified"]:
        warnings.append("quality_unverified")
    with localcontext() as context:
        context.prec = 128
        points = [
            Point(
                session_date=day,
                values=[
                    _values(rows[day], rows[baseline][0]) if day in rows else None for rows in maps
                ],
            )
            for day in days
        ]
        evidence = []
        for source, identity, rows, digest in zip(
            request.sources, identities, maps, hashes, strict=True
        ):
            first, latest = min(rows), max(rows)
            plotted = sum(day >= baseline for day in rows)
            evidence.append(
                Evidence(
                    key=source.key,
                    identity=source,
                    adjustment=identity.adjustment,
                    return_semantics=identity.return_semantics,
                    frequency=identity.frequency,
                    units=identity.units,
                    snapshot_sha256=digest,
                    first_date=first,
                    latest_date=latest,
                    baseline_close=rows[baseline][1],
                    common_end_close=rows[common_end][1],
                    common_end_return_pct=_values(
                        rows[common_end], rows[baseline][0]
                    ).cumulative_return_pct,
                    latest_close=rows[latest][1],
                    observations=len(rows),
                    missing_sessions=len(days) - plotted,
                    nonshared_sessions=plotted - len(shared),
                )
            )
    if any(
        source.first_date != request.start
        or source.latest_date != request.end
        or source.missing_sessions
        or source.nonshared_sessions
        for source in evidence
    ):
        warnings.append("coverage_gaps")
    fingerprint = _fingerprint(
        {"revision": REVISION, "request": request.model_dump(mode="json"), "sources": hashes}
    )
    return ComparisonResponse(
        request=request,
        baseline_date=baseline,
        last_common_date=common_end,
        shared_sessions=len(shared),
        sources=evidence,
        points=points,
        fingerprint_sha256=fingerprint,
        output_sha256=_fingerprint([point.model_dump(mode="json") for point in points]),
        warnings=warnings,
    )
