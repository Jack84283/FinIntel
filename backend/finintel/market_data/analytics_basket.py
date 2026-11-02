"""Single-snapshot, read-only multi-index comparisons on common stored sessions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from finintel.market_data.analytics import (
    ALLOWED_WINDOWS,
    CALCULATION_VERSION,
    AnalyticsError,
    compare_series,
    compute_series,
    validated_observations,
)
from finintel.market_data.analytics_store import _quality_status, _read_series
from finintel.market_data.index_history import INDEX_SERIES
from finintel.storage.db import StorageError, _read_connection, database_path

MAX_BASKET = 4


def run_index_basket(
    *,
    codes: list[str],
    start: date,
    end: date,
    window: int = 20,
    quality_policy: str = "require_complete",
    db: Path | None = None,
) -> dict:
    """Compute all inputs under one SQLite read transaction, without filling gaps."""
    if not 2 <= len(codes) <= MAX_BASKET or len(set(codes)) != len(codes):
        raise AnalyticsError("Select between 2 and 4 distinct index codes")
    if any(code not in INDEX_SERIES for code in codes):
        raise AnalyticsError("Unsupported index code")
    if "VIX" in codes:
        raise AnalyticsError("VIX volatility levels cannot be mixed with price-index returns")
    if window not in ALLOWED_WINDOWS:
        raise AnalyticsError("Unsupported rolling window")
    if start > end or end > datetime.now(UTC).date():
        raise AnalyticsError("Invalid or future date range")
    if quality_policy not in {"require_complete", "allow_unverified"}:
        raise AnalyticsError("Unsupported data-quality policy")
    path = db if db is not None else database_path()
    conn = _read_connection(path)
    if conn is None:
        raise AnalyticsError("No local observations are available")
    try:
        # A single SQLite snapshot is required even when an ingestion is running.
        conn.execute("BEGIN")
        inputs = [
            _read_series(conn, kind="index", code=code, provider="fred", start=start, end=end)
            for code in codes
        ]
        quality = _quality_status(conn, codes)
        if quality_policy == "require_complete" and not quality["quality_verified"]:
            raise AnalyticsError(
                "Current complete Data Quality scan with no unresolved findings required; "
                "rerun validation or explicitly allow_unverified"
            )
        observations = [validated_observations(rows) for _, rows, _ in inputs]
        shared = set(p.session_date for p in observations[0])
        for series in observations[1:]:
            shared.intersection_update(p.session_date for p in series)
        dates = sorted(shared)
        if len(dates) < 2:
            raise AnalyticsError("At least two shared observed sessions are required")
        aligned = []
        series_metadata = []
        for (identity, _, provenance), originals in zip(inputs, observations, strict=True):
            by_day = {item.session_date: item for item in originals}
            selected = [by_day[day] for day in dates]
            aligned.append(selected)
            series_metadata.append(
                {
                    "identity": identity.__dict__,
                    "source": provenance,
                    "stored_sessions": len(originals),
                    "excluded_nonshared_sessions": len(originals) - len(dates),
                }
            )
        computed = [
            compute_series(identity, rows, window=window)
            for (identity, _, _), rows in zip(inputs, aligned, strict=True)
        ]
        comparisons = [
            compare_series(inputs[0][0], aligned[0], inputs[i][0], aligned[i], window=window)
            for i in range(1, len(codes))
        ]
        points = []
        for i, day in enumerate(dates):
            values = {}
            for index, code in enumerate(codes):
                source = computed[index]["points"][i]
                relative = comparisons[index - 1]["points"][i] if index else None
                values[code] = {
                    "normalized_100": source["normalized_100"],
                    "simple_return": source["simple_return"],
                    "drawdown": source["drawdown"],
                    "rolling_volatility_annualized": source["rolling_volatility_annualized"],
                    "relative_strength": relative["relative_strength"] if relative else None,
                    "rolling_correlation": relative["rolling_correlation"] if relative else None,
                }
            points.append({"session_date": day.isoformat(), "values": values})
        conn.commit()
        return {
            "calculation_version": CALCULATION_VERSION,
            "codes": codes,
            "baseline_code": codes[0],
            "request": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "window_sessions": window,
            },
            "quality": quality,
            "quality_policy": quality_policy,
            "unverified_data": not quality["quality_verified"],
            "alignment": "intersection",
            "common_sessions": len(points),
            "first_common_session": dates[0].isoformat(),
            "last_common_session": dates[-1].isoformat(),
            "series": series_metadata,
            "points": points,
            "read_only": True,
            "no_network": True,
            "no_synthetic_sessions": True,
            "notes": [
                "All returns, drawdowns and rolling metrics are recomputed on the same "
                "intersection of stored sessions, not independently aligned charts.",
                "Missing dates are never forward-filled; consecutive shared sessions may "
                "be more than one exchange session apart.",
                "Daily price-index levels exclude dividend reinvestment and are not total returns.",
            ],
        }
    except sqlite3.Error as exc:
        raise StorageError("Local index basket query failed") from exc
    finally:
        conn.close()
