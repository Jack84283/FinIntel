"""Durable, local-first historical data-quality candidates.

The scanner never changes market observations.  It records deterministic
candidate findings with redacted evidence, and review actions are append-only
history plus a current review state.
"""

import heapq
import json
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import groupby
from pathlib import Path

from finintel.market_data.anomaly_quality import (
    ANOMALY_RULE_VERSION,
    AnomalyThresholds,
    invalid_ohlcv_fields,
    normalize_thresholds,
    parse_timestamp,
    price_jump_candidate,
    thresholds_identity,
    volume_spike_candidate,
    zero_volume_candidate,
)
from finintel.storage.db import _read_connection, _write_connection, serialized_storage_write

FINDING_TYPES = (
    "missing_session",
    "price_anomaly",
    "ohlcv_anomaly",
    "provider_divergence",
    "duplicate",
    "stale_observation",
)
REVIEW_STATES = ("candidate", "confirmed", "rejected", "accepted_correction")
SEVERITIES = ("info", "warning", "error")
CONFIDENCES = ("low", "medium", "high")
# Large enough to amortize SQLite connection setup on long histories while
# still keeping each read transaction bounded and disposable.
_SCAN_BATCH_SIZE = 8192
_SCAN_KEY = "all"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(
    *,
    instrument_kind: str,
    series_id: int | None,
    code: str,
    provider: str,
    session_date: str | None,
    finding_type: str,
    rule: str,
    adjustment: str | None = None,
    frequency: str | None = None,
) -> str:
    identity = {
        "instrument_kind": instrument_kind,
        "series_id": series_id,
        "code": code,
        "provider": provider,
        "session_date": session_date,
        "finding_type": finding_type,
        "rule": rule,
        "adjustment": adjustment,
        "frequency": frequency,
    }
    value = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(value.encode("utf-8")).hexdigest()


def _candidate(
    *,
    instrument_kind: str = "equity",
    series_id: int | None,
    code: str,
    provider: str,
    session_date: str | None,
    finding_type: str,
    severity: str,
    confidence: str,
    rule: str,
    evidence: dict,
    adjustment: str | None = None,
    frequency: str | None = None,
) -> dict:
    return {
        "instrument_kind": instrument_kind,
        "code": code,
        "series_id": series_id,
        "provider": provider,
        "session_date": session_date,
        "finding_type": finding_type,
        "severity": severity,
        "confidence": confidence,
        "rule": rule,
        "evidence": evidence,
        "adjustment": adjustment,
        "frequency": frequency,
        "fingerprint": _fingerprint(
            instrument_kind=instrument_kind,
            series_id=series_id,
            code=code,
            provider=provider,
            session_date=session_date,
            finding_type=finding_type,
            rule=rule,
            adjustment=adjustment,
            frequency=frequency,
        ),
    }


def _decimal(value: object) -> object:
    from decimal import Decimal, InvalidOperation

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _ingestion_anomaly_candidates(
    path: Path,
    *,
    instrument_kind: str,
    series_id: int | None,
    code: str,
    provider: str,
    adjustment: str,
    frequency: str,
    latest_session: str | None,
    as_of: date,
    thresholds: AnomalyThresholds,
    index: bool = False,
) -> list[dict]:
    """Build metadata candidates without retaining raw provider payloads."""

    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if index:
            rows = conn.execute(
                "SELECT id,retrieved_at,observation_sha256,"
                "COALESCE(payload_sha256,observation_sha256) AS payload_sha256,"
                "first_session,last_session,observations,updated,unchanged "
                "FROM index_ingestion_runs WHERE index_code=? AND provider=? "
                "ORDER BY retrieved_at,id",
                (code, provider),
            ).fetchall()
            latest_observation = conn.execute(
                "SELECT session_date,retrieved_at FROM index_daily_closes "
                "WHERE index_code=? AND provider=? ORDER BY session_date DESC LIMIT 1",
                (code, provider),
            ).fetchone()
        else:
            rows = conn.execute(
                "SELECT id,retrieved_at,provider_last_refreshed,payload_sha256,"
                "first_session,last_session,observations,stale "
                "FROM ingestion_runs WHERE series_id=? ORDER BY retrieved_at,id",
                (series_id,),
            ).fetchall()
            latest_observation = conn.execute(
                "SELECT session_date,retrieved_at FROM daily_prices "
                "WHERE series_id=? ORDER BY session_date DESC LIMIT 1",
                (series_id,),
            ).fetchone()
    finally:
        conn.close()

    if not rows and latest_observation is None:
        return []
    if latest_observation is not None:
        latest_session = str(latest_observation["session_date"])
    elif latest_session is None:
        latest_session = str(rows[-1]["last_session"])
    policy = thresholds_identity(thresholds)
    candidates: list[dict] = []
    payload_groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        payload = row["payload_sha256"]
        if payload:
            payload_groups.setdefault(str(payload), []).append(row)
    for payload, occurrences in payload_groups.items():
        if len(occurrences) < 2:
            continue
        candidates.append(
            _candidate(
                instrument_kind=instrument_kind,
                series_id=series_id,
                code=code,
                provider=provider,
                session_date=None,
                finding_type="duplicate",
                severity="info",
                confidence="high",
                rule="repeated_ingestion_payload",
                adjustment=adjustment,
                frequency=frequency,
                evidence={
                    "anomaly_rule_version": ANOMALY_RULE_VERSION,
                    "candidate_only": True,
                    "duplicate_source_observations": True,
                    "payload_sha256": payload,
                    "occurrences": len(occurrences),
                    "ingestion_ids": [int(row["id"]) for row in occurrences],
                    "retrieved_at": [str(row["retrieved_at"]) for row in occurrences],
                    "first_session": str(occurrences[0]["first_session"]),
                    "last_session": str(occurrences[-1]["last_session"]),
                    "thresholds": policy,
                },
            )
        )

    revision_events: list[dict[str, object]] = []
    stale_imports: list[dict[str, object]] = []
    newest_provider_revision = None
    previous_index_observation = None
    previous_index_window = None
    for row in rows:
        if not index:
            stale_count = int(row["stale"] or 0)
            if stale_count:
                stale_imports.append(
                    {
                        "ingestion_id": int(row["id"]),
                        "stale_observations": stale_count,
                        "retrieved_at": str(row["retrieved_at"]),
                        "provider_last_refreshed": row["provider_last_refreshed"],
                    }
                )
            provider_revision = parse_timestamp(row["provider_last_refreshed"])
            if provider_revision is not None:
                if (
                    newest_provider_revision is not None
                    and provider_revision < newest_provider_revision[0]
                ):
                    revision_events.append(
                        {
                            "ingestion_id": int(row["id"]),
                            "provider_last_refreshed": provider_revision.isoformat(),
                            "newer_revision": newest_provider_revision[1],
                        }
                    )
                if (
                    newest_provider_revision is None
                    or provider_revision > newest_provider_revision[0]
                ):
                    newest_provider_revision = (provider_revision, provider_revision.isoformat())
        else:
            observation_hash = row["observation_sha256"]
            window = (row["first_session"], row["last_session"])
            if (
                observation_hash
                and previous_index_observation
                and observation_hash != previous_index_observation
                and window == previous_index_window
            ):
                revision_events.append(
                    {
                        "ingestion_id": int(row["id"]),
                        "observation_revision": str(observation_hash),
                        "previous_observation_revision": str(previous_index_observation),
                        "window": list(window),
                    }
                )
            previous_index_observation = observation_hash
            previous_index_window = window

    if stale_imports or revision_events:
        candidates.append(
            _candidate(
                instrument_kind=instrument_kind,
                series_id=series_id,
                code=code,
                provider=provider,
                session_date=latest_session,
                finding_type="stale_observation",
                severity="warning",
                confidence="medium",
                rule="outdated_provider_revision",
                adjustment=adjustment,
                frequency=frequency,
                evidence={
                    "anomaly_rule_version": ANOMALY_RULE_VERSION,
                    "candidate_only": True,
                    "stale_imports": stale_imports,
                    "revision_events": revision_events,
                    "revision_status": "provider_revision_requires_review",
                    "thresholds": policy,
                },
            )
        )

    latest = rows[-1] if rows else None
    latest_retrieved = parse_timestamp(latest["retrieved_at"]) if latest is not None else None
    latest_provider_revision = (
        None if index or latest is None else parse_timestamp(latest["provider_last_refreshed"])
    )
    latest_observation_retrieved = (
        parse_timestamp(latest_observation["retrieved_at"])
        if latest_observation is not None
        else None
    )
    as_of_timestamp = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    stale_reasons = []
    if latest_retrieved is not None and as_of_timestamp - latest_retrieved > timedelta(
        days=thresholds.stale_after_days
    ):
        stale_reasons.append("latest_ingestion_retrieved_at")
    if (
        latest_provider_revision is not None
        and as_of_timestamp - latest_provider_revision > timedelta(days=thresholds.stale_after_days)
    ):
        stale_reasons.append("provider_last_refreshed")
    if (
        latest_observation_retrieved is not None
        and as_of_timestamp - latest_observation_retrieved
        > timedelta(days=thresholds.stale_after_days)
    ):
        stale_reasons.append("latest_observation_retrieved_at")
    if stale_reasons:
        candidates.append(
            _candidate(
                instrument_kind=instrument_kind,
                series_id=series_id,
                code=code,
                provider=provider,
                session_date=latest_session,
                finding_type="stale_observation",
                severity="warning",
                confidence="low",
                rule="stale_latest_observation",
                adjustment=adjustment,
                frequency=frequency,
                evidence={
                    "anomaly_rule_version": ANOMALY_RULE_VERSION,
                    "candidate_only": True,
                    "stale_after_days": thresholds.stale_after_days,
                    "stale_reasons": stale_reasons,
                    "latest_retrieved_at": (
                        str(latest["retrieved_at"]) if latest is not None else None
                    ),
                    "latest_observation_retrieved_at": (
                        str(latest_observation["retrieved_at"])
                        if latest_observation is not None
                        else None
                    ),
                    "provider_last_refreshed": (
                        str(latest["provider_last_refreshed"])
                        if not index
                        and latest is not None
                        and latest["provider_last_refreshed"] is not None
                        else None
                    ),
                    "as_of": as_of.isoformat(),
                    "thresholds": policy,
                },
            )
        )
    return candidates


def _iter_query_rows(
    path: Path,
    *,
    select_sql: str,
    order_expression: str,
    key_columns: tuple[str, ...],
    batch_size: int = _SCAN_BATCH_SIZE,
    start_key: tuple[object, ...] | None = None,
    start_inclusive: bool = False,
) -> Iterator[sqlite3.Row]:
    """Yield bounded keyset pages so no reader remains open for the full scan."""

    last_key: tuple[object, ...] | None = None
    while True:
        conn = _read_connection(path, snapshot=False)
        if conn is None:
            raise FileNotFoundError(path)
        try:
            parameters: list[object] = []
            if last_key is None:
                if start_key is None:
                    query = f"{select_sql} ORDER BY {order_expression} LIMIT ?"
                else:
                    placeholders = ", ".join("?" for _ in key_columns)
                    operator = ">=" if start_inclusive else ">"
                    query = (
                        f"{select_sql} WHERE ({order_expression}) {operator} "
                        f"({placeholders}) ORDER BY {order_expression} LIMIT ?"
                    )
                    parameters.extend(start_key)
            else:
                placeholders = ", ".join("?" for _ in key_columns)
                query = (
                    f"{select_sql} WHERE ({order_expression}) > ({placeholders}) "
                    f"ORDER BY {order_expression} LIMIT ?"
                )
                parameters.extend(last_key)
            parameters.append(batch_size)
            rows = conn.execute(query, parameters).fetchall()
        finally:
            conn.close()
        if not rows:
            return
        yield from rows
        last_key = tuple(rows[-1][column] for column in key_columns)
        if len(rows) < batch_size:
            return


def _iter_series_prices(
    path: Path, series: sqlite3.Row, *, start_session: str | None = None
) -> Iterator[dict]:
    """Read one series through its primary-key order without a join sort."""

    last_session = start_session
    while True:
        conn = _read_connection(path, snapshot=False)
        if conn is None:
            raise FileNotFoundError(path)
        try:
            parameters: list[object] = [series["id"]]
            query = "SELECT session_date, close FROM daily_prices WHERE series_id = ?"
            if last_session is not None:
                query += " AND session_date > ?"
                parameters.append(last_session)
            query += " ORDER BY session_date LIMIT ?"
            parameters.append(_SCAN_BATCH_SIZE)
            rows = conn.execute(query, parameters).fetchall()
        finally:
            conn.close()
        if not rows:
            return
        for row in rows:
            yield {
                "series_id": series["id"],
                "symbol": series["symbol"],
                "provider": series["provider"],
                "frequency": series["frequency"],
                "adjustment": series["adjustment"],
                "session_date": row["session_date"],
                "close": row["close"],
            }
        last_session = rows[-1]["session_date"]
        if len(rows) < _SCAN_BATCH_SIZE:
            return


def _iter_cross_provider_rows(
    path: Path, *, start_key: dict[str, str] | None = None
) -> Iterator[dict]:
    """Merge per-series primary-key streams in Python instead of SQL-sorting bars."""

    series_start_key = None
    if start_key is not None:
        # Start at the first provider for the checkpoint's instrument group;
        # the per-series streams below then seek past the completed session.
        series_start_key = (
            start_key["symbol"],
            start_key["frequency"],
            start_key["adjustment"],
            "",
            -1,
        )
    series_rows = _iter_query_rows(
        path,
        select_sql=("SELECT id, symbol, provider, frequency, adjustment FROM series"),
        order_expression="symbol, frequency, adjustment, provider, id",
        key_columns=("symbol", "frequency", "adjustment", "provider", "id"),
        start_key=series_start_key,
        start_inclusive=True,
    )
    for (symbol, frequency, adjustment), grouped in groupby(
        series_rows,
        key=lambda row: (row["symbol"], row["frequency"], row["adjustment"]),
    ):
        series_group = list(grouped)
        group_start_session = (
            start_key["session_date"]
            if start_key is not None
            and (symbol, frequency, adjustment)
            == (start_key["symbol"], start_key["frequency"], start_key["adjustment"])
            else None
        )
        streams = [
            _iter_series_prices(path, series, start_session=group_start_session)
            for series in series_group
        ]
        yield from heapq.merge(
            *streams,
            key=lambda row: (row["session_date"], row["provider"], row["series_id"]),
        )


def _equity_cursor_previous(path: Path, source_key: dict[str, object] | None):
    if source_key is None:
        return None
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        row = conn.execute(
            "SELECT session_date, close, volume FROM daily_prices "
            "WHERE series_id = ? AND session_date = ?",
            (source_key["series_id"], source_key["session_date"]),
        ).fetchone()
        history_rows = conn.execute(
            "SELECT volume FROM daily_prices WHERE series_id = ? AND session_date < ? "
            "ORDER BY session_date DESC LIMIT 20",
            (source_key["series_id"], source_key["session_date"]),
        ).fetchall()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        previous_date = date.fromisoformat(row["session_date"])
    except (TypeError, ValueError):
        return None
    previous_close = _decimal(row["close"])
    if previous_close is None:
        return None
    history = [item[0] for item in history_rows]
    return previous_date, previous_close, row["session_date"], row["volume"], history


def _index_cursor_previous(path: Path, source_key: dict[str, object] | None):
    if source_key is None:
        return None
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        row = conn.execute(
            "SELECT session_date, close FROM index_daily_closes "
            "WHERE index_code = ? AND provider = ? AND session_date = ?",
            (source_key["symbol"], source_key["provider"], source_key["session_date"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        previous_date = date.fromisoformat(row["session_date"])
    except (TypeError, ValueError):
        return None
    previous_close = _decimal(row["close"])
    if previous_close is None:
        return None
    return previous_date, previous_close


def _read_candidates(
    path: Path,
    max_findings: int,
    *,
    checkpoint: dict[str, object] | None = None,
    thresholds: AnomalyThresholds | Mapping[str, object] | None = None,
    as_of: date | None = None,
) -> tuple[list[dict], int, dict[str, object], bool]:
    thresholds = normalize_thresholds(thresholds)
    as_of = as_of or datetime.now(UTC).date()
    checkpoint = checkpoint or {"phase": "equity", "source_key": None}
    checkpoint_phase = str(checkpoint.get("phase", "equity"))
    checkpoint_key = checkpoint.get("source_key")
    if not isinstance(checkpoint_key, dict):
        checkpoint_key = None
    if checkpoint_phase not in {"equity", "index", "cross_provider", "complete"}:
        checkpoint_phase = "equity"
        checkpoint_key = None

    equity_start = None
    index_start = None
    cross_start = None
    if checkpoint_phase == "equity" and checkpoint_key is not None:
        equity_start = (
            checkpoint_key["series_id"],
            checkpoint_key["session_date"],
        )
    elif checkpoint_phase == "index" and checkpoint_key is not None:
        index_start = (
            checkpoint_key["symbol"],
            checkpoint_key["provider"],
            checkpoint_key["session_date"],
        )
    elif checkpoint_phase == "cross_provider" and checkpoint_key is not None:
        cross_start = checkpoint_key

    rows = _iter_query_rows(
        path,
        select_sql=(
            "SELECT s.id AS series_id, s.symbol, s.provider, s.frequency, s.adjustment, "
            "dp.session_date, dp.open, dp.high, dp.low, dp.close, dp.volume, "
            "dp.ingestion_id, dp.retrieved_at "
            "FROM series AS s JOIN daily_prices AS dp ON dp.series_id = s.id"
        ),
        order_expression="s.id, dp.session_date",
        key_columns=("series_id", "session_date"),
        start_key=equity_start,
    )
    index_rows = _iter_query_rows(
        path,
        select_sql=(
            "SELECT index_code AS symbol, provider, session_date, close FROM index_daily_closes"
        ),
        order_expression="index_code, provider, session_date",
        key_columns=("symbol", "provider", "session_date"),
        start_key=index_start,
    )

    # These cursors are consumed in indexed order. Only one series (and later
    # one overlapping provider/session group) is retained at a time; the full
    # price database is never materialized in memory.
    findings: list[dict] = []
    findings_examined = 0
    page_full = False
    resume_cursor: dict[str, object] = checkpoint
    current_source_key: dict[str, object] | None = None
    current_source_total = 0
    current_source_stored = 0
    current_source_was_full = False

    def begin_source(source_key: dict[str, object]) -> None:
        nonlocal current_source_key, current_source_total, current_source_stored
        nonlocal current_source_was_full
        current_source_key = source_key
        current_source_total = 0
        current_source_stored = 0
        current_source_was_full = page_full

    def finish_source() -> None:
        nonlocal resume_cursor
        if (
            current_source_key is not None
            and not current_source_was_full
            and current_source_stored == current_source_total
        ):
            resume_cursor = {
                "phase": current_source_key["phase"],
                "source_key": {
                    key: value for key, value in current_source_key.items() if key != "phase"
                },
            }

    def add(item: dict) -> bool:
        nonlocal findings_examined, current_source_total, current_source_stored, page_full
        findings_examined += 1
        current_source_total += 1
        # Finish the current source item even if it takes the page a little
        # over its cap. This keeps the traversal cursor after a complete item
        # instead of losing a sibling candidate from the same observation.
        if len(findings) >= max_findings and current_source_was_full:
            page_full = True
            return False
        findings.append(item)
        current_source_stored += 1
        if len(findings) >= max_findings:
            page_full = True
        return True

    if checkpoint_phase == "equity":
        for series_id, series_rows in groupby(rows, key=lambda row: row["series_id"]):
            previous: tuple[date, object, str, int | None] | None = None
            volume_history: list[object] = []
            if checkpoint_key is not None and series_id == checkpoint_key.get("series_id"):
                context = _equity_cursor_previous(path, checkpoint_key)
                if context is not None:
                    previous = context[:4]
                    volume_history = list(context[4])
            metadata_emitted = False
            for row in series_rows:
                begin_source(
                    {"phase": "equity", "series_id": series_id, "session_date": row["session_date"]}
                )
                if not metadata_emitted:
                    metadata_was_processed = (
                        checkpoint_phase == "equity"
                        and checkpoint_key is not None
                        and series_id == checkpoint_key.get("series_id")
                    )
                    if not metadata_was_processed:
                        for item in _ingestion_anomaly_candidates(
                            path,
                            instrument_kind="equity",
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            adjustment=row["adjustment"],
                            frequency=row["frequency"],
                            latest_session=None,
                            as_of=as_of,
                            thresholds=thresholds,
                        ):
                            add(item)
                    metadata_emitted = True
                session_date = row["session_date"]
                try:
                    current_date = date.fromisoformat(session_date)
                except (TypeError, ValueError):
                    add(
                        _candidate(
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            adjustment=row["adjustment"],
                            frequency=row["frequency"],
                            session_date=session_date,
                            finding_type="ohlcv_anomaly",
                            severity="error",
                            confidence="high",
                            rule="invalid_session_date",
                            evidence={"session_date": str(session_date)},
                        )
                    )
                    finish_source()
                    if page_full:
                        break
                    continue

                values = {name: _decimal(row[name]) for name in ("open", "high", "low", "close")}
                volume = row["volume"]
                invalid = invalid_ohlcv_fields(values, volume)
                if invalid:
                    add(
                        _candidate(
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            adjustment=row["adjustment"],
                            frequency=row["frequency"],
                            session_date=session_date,
                            finding_type="ohlcv_anomaly",
                            severity="error",
                            confidence="high",
                            rule="validated_ohlcv_constraints",
                            evidence={
                                "anomaly_rule_version": ANOMALY_RULE_VERSION,
                                "candidate_only": True,
                                "invalid_fields": invalid,
                            },
                        )
                    )

                try:
                    volume_value = int(volume)
                except (TypeError, ValueError):
                    volume_value = None

                close = values["close"]
                if previous:
                    jump = price_jump_candidate(
                        previous[1],
                        close,
                        adjustment=row["adjustment"],
                        thresholds=thresholds,
                    )
                    if jump is not None:
                        add(
                            _candidate(
                                series_id=series_id,
                                code=row["symbol"],
                                provider=row["provider"],
                                adjustment=row["adjustment"],
                                frequency=row["frequency"],
                                session_date=session_date,
                                finding_type="price_anomaly",
                                severity=jump["severity"],
                                confidence=jump["confidence"],
                                rule=jump["rule"],
                                evidence={
                                    **jump["evidence"],
                                    "previous_session": previous[0].isoformat(),
                                },
                            )
                        )

                spike = volume_spike_candidate(volume_value, volume_history, thresholds=thresholds)
                if spike is not None:
                    add(
                        _candidate(
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            adjustment=row["adjustment"],
                            frequency=row["frequency"],
                            session_date=session_date,
                            finding_type="ohlcv_anomaly",
                            severity=spike["severity"],
                            confidence=spike["confidence"],
                            rule=spike["rule"],
                            evidence=spike["evidence"],
                        )
                    )
                zero_volume = zero_volume_candidate(
                    previous[1] if previous else None,
                    close,
                    volume_value,
                    volume_history,
                    thresholds=thresholds,
                )
                if zero_volume is not None:
                    add(
                        _candidate(
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            adjustment=row["adjustment"],
                            frequency=row["frequency"],
                            session_date=session_date,
                            finding_type="ohlcv_anomaly",
                            severity=zero_volume["severity"],
                            confidence=zero_volume["confidence"],
                            rule=zero_volume["rule"],
                            evidence=zero_volume["evidence"],
                        )
                    )

                if previous and current_date > previous[0] + timedelta(days=1):
                    missing_weekdays = []
                    cursor = previous[0] + timedelta(days=1)
                    while cursor < current_date:
                        if cursor.weekday() < 5:
                            missing_weekdays.append(cursor.isoformat())
                        cursor += timedelta(days=1)
                    if missing_weekdays:
                        add(
                            _candidate(
                                series_id=series_id,
                                code=row["symbol"],
                                provider=row["provider"],
                                adjustment=row["adjustment"],
                                frequency=row["frequency"],
                                session_date=current_date.isoformat(),
                                finding_type="missing_session",
                                severity="warning",
                                confidence="low",
                                rule="weekday_gap_candidate",
                                evidence={
                                    "previous_session": previous[0].isoformat(),
                                    "next_session": current_date.isoformat(),
                                    "weekday_candidates": missing_weekdays[:31],
                                    "calendar_applied": False,
                                },
                            )
                        )
                if volume_value is not None and volume_value >= 0:
                    volume_history.append(volume_value)
                    del volume_history[: -thresholds.volume_lookback]
                if close is not None and close > 0:
                    previous = (current_date, close, row["session_date"], volume_value)
                finish_source()
                if page_full:
                    break
            if page_full:
                break

    if not page_full and checkpoint_phase in {"equity", "index"}:
        for (index_code, provider), index_series in groupby(
            index_rows, key=lambda row: (row["symbol"], row["provider"])
        ):
            previous: tuple[date, object] | None = None
            if (
                checkpoint_phase == "index"
                and checkpoint_key is not None
                and index_code == checkpoint_key.get("symbol")
                and provider == checkpoint_key.get("provider")
            ):
                previous = _index_cursor_previous(path, checkpoint_key)
            metadata_emitted = False
            for row in index_series:
                begin_source(
                    {
                        "phase": "index",
                        "symbol": index_code,
                        "provider": provider,
                        "session_date": row["session_date"],
                    }
                )
                if not metadata_emitted:
                    metadata_was_processed = (
                        checkpoint_phase == "index"
                        and checkpoint_key is not None
                        and index_code == checkpoint_key.get("symbol")
                        and provider == checkpoint_key.get("provider")
                    )
                    if not metadata_was_processed:
                        for item in _ingestion_anomaly_candidates(
                            path,
                            instrument_kind="index",
                            series_id=None,
                            code=index_code,
                            provider=provider,
                            adjustment="price_index",
                            frequency="1d",
                            latest_session=None,
                            as_of=as_of,
                            thresholds=thresholds,
                            index=True,
                        ):
                            add(item)
                    metadata_emitted = True
                session_date = row["session_date"]
                try:
                    current_date = date.fromisoformat(session_date)
                except (TypeError, ValueError):
                    add(
                        _candidate(
                            instrument_kind="index",
                            series_id=None,
                            code=index_code,
                            provider=provider,
                            session_date=session_date,
                            finding_type="price_anomaly",
                            severity="error",
                            confidence="high",
                            rule="invalid_index_session_date",
                            evidence={"session_date": str(session_date)},
                        )
                    )
                    finish_source()
                    if page_full:
                        break
                    continue
                close = _decimal(row["close"])
                if close is None or close <= 0:
                    add(
                        _candidate(
                            instrument_kind="index",
                            series_id=None,
                            code=index_code,
                            provider=provider,
                            session_date=session_date,
                            finding_type="price_anomaly",
                            severity="error",
                            confidence="high",
                            rule="positive_finite_index_close",
                            evidence={"close": str(row["close"])},
                        )
                    )
                if previous:
                    jump = price_jump_candidate(
                        previous[1],
                        close,
                        adjustment="price_index",
                        thresholds=thresholds,
                    )
                    if jump is not None:
                        add(
                            _candidate(
                                instrument_kind="index",
                                series_id=None,
                                code=index_code,
                                provider=provider,
                                adjustment="price_index",
                                frequency="1d",
                                session_date=session_date,
                                finding_type="price_anomaly",
                                severity=jump["severity"],
                                confidence=jump["confidence"],
                                rule=jump["rule"],
                                evidence={
                                    **jump["evidence"],
                                    "previous_session": previous[0].isoformat(),
                                },
                            )
                        )
                if previous and current_date > previous[0] + timedelta(days=1):
                    missing_weekdays = []
                    cursor = previous[0] + timedelta(days=1)
                    while cursor < current_date:
                        if cursor.weekday() < 5:
                            missing_weekdays.append(cursor.isoformat())
                        cursor += timedelta(days=1)
                    if missing_weekdays:
                        add(
                            _candidate(
                                instrument_kind="index",
                                series_id=None,
                                code=index_code,
                                provider=provider,
                                session_date=current_date.isoformat(),
                                finding_type="missing_session",
                                severity="warning",
                                confidence="low",
                                rule="weekday_gap_candidate",
                                evidence={
                                    "previous_session": previous[0].isoformat(),
                                    "next_session": current_date.isoformat(),
                                    "weekday_candidates": missing_weekdays[:31],
                                    "calendar_applied": False,
                                },
                            )
                        )
                if close is not None and close > 0:
                    previous = (current_date, close)
                finish_source()
                if page_full:
                    break
            if page_full:
                break

    if not page_full and checkpoint_phase in {"equity", "index", "cross_provider"}:
        provider_rows = _iter_cross_provider_rows(path, start_key=cross_start)
        for (symbol, frequency, adjustment, session), observation_group in groupby(
            provider_rows,
            key=lambda row: (
                row["symbol"],
                row["frequency"],
                row["adjustment"],
                row["session_date"],
            ),
        ):
            begin_source(
                {
                    "phase": "cross_provider",
                    "symbol": symbol,
                    "frequency": frequency,
                    "adjustment": adjustment,
                    "session_date": session,
                }
            )
            observations = list(observation_group)
            if len(observations) >= 2:
                valid = [(row, _decimal(row["close"])) for row in observations]
                valid = [(row, close) for row, close in valid if close is not None and close > 0]
                for index, (left, left_close) in enumerate(valid):
                    for right, right_close in valid[index + 1 :]:
                        ratio = abs(left_close - right_close) / left_close
                        if ratio <= 0.01:
                            continue
                        provider = "|".join(sorted((left["provider"], right["provider"])))
                        add(
                            _candidate(
                                series_id=left["series_id"],
                                code=symbol,
                                provider=provider,
                                adjustment=adjustment,
                                frequency=frequency,
                                session_date=session,
                                finding_type="provider_divergence",
                                severity="warning",
                                confidence="low",
                                rule="overlapping_close_differs_over_1pct",
                                evidence={
                                    "adjustment": adjustment,
                                    "left_provider": left["provider"],
                                    "right_provider": right["provider"],
                                    "left_close": str(left_close),
                                    "right_close": str(right_close),
                                    "relative_difference": str(ratio),
                                },
                            )
                        )
            finish_source()
            if page_full:
                break

    return findings, findings_examined, resume_cursor, not page_full


@serialized_storage_write
def _ensure_quality_schema(path: Path) -> None:
    """Run any required migrations while holding the short write lock."""

    connection = _write_connection(path)
    connection.close()


@serialized_storage_write
def _persist(path: Path, candidates: list[dict]) -> None:
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN")
        now = _iso_now()
        for item in candidates:
            # PR #38 used the fixed threshold rule name. Upgrade that row to
            # the configurable rule identity before the normal upsert so a
            # scan after this feature does not strand its review history on a
            # duplicate finding.
            if item["rule"] == "price_jump_candidate":
                legacy_fingerprint = _fingerprint(
                    instrument_kind=item["instrument_kind"],
                    series_id=item["series_id"],
                    code=item["code"],
                    provider=item["provider"],
                    session_date=item["session_date"],
                    finding_type=item["finding_type"],
                    rule="close_jump_over_2x_or_under_half",
                    adjustment=(
                        item["adjustment"] if item["instrument_kind"] == "equity" else None
                    ),
                    frequency=(item["frequency"] if item["instrument_kind"] == "equity" else None),
                )
                if legacy_fingerprint != item["fingerprint"]:
                    legacy_row = conn.execute(
                        "SELECT id FROM data_quality_findings WHERE fingerprint=?",
                        (legacy_fingerprint,),
                    ).fetchone()
                    current_row = conn.execute(
                        "SELECT id FROM data_quality_findings WHERE fingerprint=?",
                        (item["fingerprint"],),
                    ).fetchone()
                    if legacy_row is not None and current_row is None:
                        conn.execute(
                            "UPDATE data_quality_findings SET fingerprint=? WHERE id=?",
                            (item["fingerprint"], legacy_row["id"]),
                        )
            conn.execute(
                "INSERT INTO data_quality_findings "
                "(instrument_kind, code, series_id, provider, session_date, finding_type, "
                "severity, confidence, rule, evidence_json, fingerprint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "severity=excluded.severity, confidence=excluded.confidence, "
                "rule=excluded.rule, evidence_json=excluded.evidence_json, "
                "updated_at=excluded.updated_at",
                (
                    item["instrument_kind"],
                    item["code"],
                    item["series_id"],
                    item["provider"],
                    item["session_date"],
                    item["finding_type"],
                    item["severity"],
                    item["confidence"],
                    item["rule"],
                    json.dumps(item["evidence"], sort_keys=True, separators=(",", ":")),
                    item["fingerprint"],
                    now,
                    now,
                ),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


@serialized_storage_write
def _persist_scan_run(
    path: Path,
    *,
    started_at: str,
    completed_at: str,
    max_findings: int,
    findings_examined: int,
    findings_stored: int,
    total_known_findings: int,
    persisted_findings_total: int,
    status: str,
    truncated: bool,
) -> tuple[int, str]:
    metadata = {
        "started_at": started_at,
        "completed_at": completed_at,
        "max_findings": max_findings,
        "findings_examined": findings_examined,
        "findings_stored": findings_stored,
        "total_known_findings": total_known_findings,
        "persisted_findings_total": persisted_findings_total,
        "status": status,
        "truncated": truncated,
    }
    run_key = sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO data_quality_scan_runs("
            "started_at,completed_at,max_findings,findings_examined,findings_stored,"
            "total_known_findings,persisted_findings_total,status,truncated,run_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                completed_at,
                max_findings,
                findings_examined,
                findings_stored,
                total_known_findings,
                persisted_findings_total,
                status,
                int(truncated),
                run_key,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM data_quality_scan_runs WHERE run_key = ?", (run_key,)
        ).fetchone()
        return int(row[0]), run_key
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _stored_finding_summary(path: Path) -> tuple[int, dict[str, int]]:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_quality_findings'"
            ).fetchone()
            is None
        ):
            return 0, {}
        total = conn.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0]
        rows = conn.execute(
            "SELECT review_state, COUNT(*) AS count FROM data_quality_findings "
            "GROUP BY review_state"
        ).fetchall()
        return int(total), {row["review_state"]: int(row["count"]) for row in rows}
    finally:
        conn.close()


def _latest_scan_run(path: Path) -> dict | None:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_quality_scan_runs'"
            ).fetchone()
            is None
        ):
            return None
        row = conn.execute(
            "SELECT id,started_at,completed_at,max_findings,findings_examined,findings_stored,"
            "total_known_findings,persisted_findings_total,status,truncated "
            ",run_key FROM data_quality_scan_runs ORDER BY completed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _source_identity(path: Path) -> str:
    conn = _read_connection(path, snapshot=False)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        facts = {
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "source_version": conn.execute(
                "SELECT source_version FROM data_quality_source_state WHERE id = 1"
            ).fetchone()[0],
            "series": conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM series").fetchone(),
            "daily_prices": conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0), "
                "COALESCE(MAX(retrieved_at), '') FROM daily_prices"
            ).fetchone(),
            "index_daily_closes": conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0), "
                "COALESCE(MAX(retrieved_at), '') FROM index_daily_closes"
            ).fetchone(),
            "ingestion_runs": conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0), "
                "COALESCE(MAX(retrieved_at), '') FROM ingestion_runs"
            ).fetchone(),
            "index_ingestion_runs": conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0), "
                "COALESCE(MAX(retrieved_at), '') FROM index_ingestion_runs"
            ).fetchone(),
        }
        normalized = {
            key: list(value) if isinstance(value, sqlite3.Row) else value
            for key, value in facts.items()
        }
        return sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    finally:
        conn.close()


def _scan_source_identity(source_identity: str, thresholds: AnomalyThresholds) -> str:
    policy = {"source_identity": source_identity, "thresholds": thresholds_identity(thresholds)}
    return sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source_data_version(path: Path) -> int | None:
    conn = _read_connection(path, snapshot=False)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='data_quality_source_state'"
            ).fetchone()
            is None
        ):
            return None
        row = conn.execute(
            "SELECT source_version FROM data_quality_source_state WHERE id = 1"
        ).fetchone()
        return int(row[0]) if row is not None else None
    finally:
        conn.close()


def _progress_checkpoint(progress: dict | None) -> dict[str, object]:
    if progress is None:
        return {"phase": "equity", "source_key": None}
    phase = progress.get("cursor_phase") or "equity"
    source_key = None
    encoded = progress.get("cursor_source_key")
    if encoded:
        try:
            decoded = json.loads(encoded)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            source_key = decoded
    return {"phase": phase, "source_key": source_key}


def _encode_source_key(checkpoint: dict[str, object]) -> str | None:
    source_key = checkpoint.get("source_key")
    if not isinstance(source_key, dict):
        return None
    return json.dumps(source_key, sort_keys=True, separators=(",", ":"))


def _scan_progress(path: Path) -> dict | None:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='data_quality_scan_progress'"
            ).fetchone()
            is None
        ):
            return None
        columns = {row[1] for row in conn.execute("PRAGMA table_info(data_quality_scan_progress)")}
        cursor_fields = (
            "cursor_phase,cursor_source_key"
            if {"cursor_phase", "cursor_source_key"}.issubset(columns)
            else "'equity' AS cursor_phase,NULL AS cursor_source_key"
        )
        row = conn.execute(
            "SELECT scan_key,source_identity,source_data_version_start,"
            "source_data_version_end,cursor_start,cursor_next,max_findings,"
            "findings_examined,findings_stored,total_known_findings,"
            "persisted_findings_total,status,stale,updated_at,last_run_key,"
            + cursor_fields
            + " FROM data_quality_scan_progress WHERE scan_key = ?",
            (_SCAN_KEY,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


@serialized_storage_write
def _persist_scan_progress(
    path: Path,
    *,
    source_identity: str,
    source_data_version_start: int,
    source_data_version_end: int,
    cursor_start: int,
    cursor_next: int,
    max_findings: int,
    findings_examined: int,
    findings_stored: int,
    total_known_findings: int,
    persisted_findings_total: int,
    status: str,
    stale: bool,
    updated_at: str,
    last_run_key: str,
    cursor_phase: str,
    cursor_source_key: str | None,
) -> None:
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO data_quality_scan_progress("
            "scan_key,source_identity,source_data_version_start,source_data_version_end,"
            "cursor_start,cursor_next,max_findings,findings_examined,findings_stored,"
            "total_known_findings,persisted_findings_total,status,stale,updated_at,last_run_key,"
            "cursor_phase,cursor_source_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scan_key) DO UPDATE SET "
            "source_identity=excluded.source_identity,"
            "source_data_version_start=excluded.source_data_version_start,"
            "source_data_version_end=excluded.source_data_version_end,"
            "cursor_start=excluded.cursor_start,cursor_next=excluded.cursor_next,"
            "max_findings=excluded.max_findings,"
            "findings_examined=excluded.findings_examined,"
            "findings_stored=excluded.findings_stored,"
            "total_known_findings=excluded.total_known_findings,"
            "persisted_findings_total=excluded.persisted_findings_total,"
            "status=excluded.status,stale=excluded.stale,updated_at=excluded.updated_at,"
            "last_run_key=excluded.last_run_key,cursor_phase=excluded.cursor_phase,"
            "cursor_source_key=excluded.cursor_source_key",
            (
                _SCAN_KEY,
                source_identity,
                source_data_version_start,
                source_data_version_end,
                cursor_start,
                cursor_next,
                max_findings,
                findings_examined,
                findings_stored,
                total_known_findings,
                persisted_findings_total,
                status,
                int(stale),
                updated_at,
                last_run_key,
                cursor_phase,
                cursor_source_key,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_quality_scan(
    path: Path,
    *,
    max_findings: int = 1000,
    thresholds: AnomalyThresholds | Mapping[str, object] | None = None,
    as_of: date | None = None,
) -> dict:
    """Scan local observations and persist candidate findings without repairing data."""

    if not 1 <= max_findings <= 10_000:
        raise ValueError("max_findings must be between 1 and 10000")
    if not path.is_file():
        raise FileNotFoundError(path)
    thresholds = normalize_thresholds(thresholds)
    as_of = as_of or datetime.now(UTC).date()
    # Quality scans may be the first write after an older database is opened.
    # Migrate before reading so legacy v1-v13 databases have all required tables.
    # Only this short migration and the later persistence transaction are locked;
    # the potentially long read-only scan does not hold the process write lock.
    started_at = _iso_now()
    _ensure_quality_schema(path)
    database_identity = _source_identity(path)
    source_identity = _scan_source_identity(database_identity, thresholds)
    source_data_version_start = _source_data_version(path)
    progress = _scan_progress(path)
    checkpoint = {"phase": "equity", "source_key": None}
    cursor_start = 0
    resuming = False
    if (
        progress is not None
        and progress["status"] == "partial"
        and not progress["stale"]
        and progress["source_identity"] == source_identity
        and progress.get("cursor_source_key")
    ):
        resuming = True
        cursor_start = int(progress["cursor_next"])
        checkpoint = _progress_checkpoint(progress)
    candidates, findings_seen, next_checkpoint, exhausted = _read_candidates(
        path,
        max_findings,
        checkpoint=checkpoint,
        thresholds=thresholds,
        as_of=as_of,
    )
    source_data_version_end = _source_data_version(path)
    source_changed = (
        source_data_version_end != source_data_version_start
        or _source_identity(path) != database_identity
    )
    _persist(path, candidates)
    findings_stored = len(candidates)
    persisted_findings_total, _ = _stored_finding_summary(path)
    previous_examined = (
        int(progress["findings_examined"]) if resuming and progress is not None else 0
    )
    findings_examined = previous_examined + findings_seen
    established_total = (
        int(progress["total_known_findings"])
        if progress is not None
        and progress["status"] == "completed"
        and progress["source_identity"] == source_identity
        and not progress["stale"]
        else 0
    )
    total_known_findings = established_total or findings_examined
    cursor_next = cursor_start + findings_stored
    stale = source_changed
    truncated = not exhausted and not stale
    complete = not truncated and not stale
    status = "completed" if complete else "partial"
    completed_at = _iso_now()
    scan_id, run_key = _persist_scan_run(
        path,
        started_at=started_at,
        completed_at=completed_at,
        max_findings=max_findings,
        findings_examined=findings_examined,
        findings_stored=findings_stored,
        total_known_findings=total_known_findings,
        persisted_findings_total=persisted_findings_total,
        status=status,
        truncated=truncated,
    )
    _persist_scan_progress(
        path,
        source_identity=source_identity,
        source_data_version_start=source_data_version_start,
        source_data_version_end=source_data_version_end,
        cursor_start=cursor_start,
        cursor_next=0 if stale or complete else cursor_next,
        max_findings=max_findings,
        findings_examined=findings_examined,
        findings_stored=findings_stored,
        total_known_findings=total_known_findings,
        persisted_findings_total=persisted_findings_total,
        status="stale" if stale else status,
        stale=stale,
        updated_at=completed_at,
        last_run_key=run_key,
        cursor_phase="equity"
        if stale
        else next_checkpoint["phase"]
        if not complete
        else "complete",
        cursor_source_key=None if stale or complete else _encode_source_key(next_checkpoint),
    )
    counts = Counter(item["finding_type"] for item in candidates)
    severity = Counter(item["severity"] for item in candidates)
    return {
        "scan_id": scan_id,
        "status": "stale" if stale else status,
        "complete": complete,
        "truncated": truncated,
        "stale": stale,
        "anomaly_rule_version": ANOMALY_RULE_VERSION,
        "thresholds": thresholds_identity(thresholds),
        "source_data_version_start": source_data_version_start,
        "source_data_version_end": source_data_version_end,
        "cursor_start": cursor_start,
        "cursor_next": 0 if stale or complete else cursor_next,
        "cursor_phase": "equity" if stale or complete else next_checkpoint["phase"],
        "cursor_source_key": None if stale or complete else next_checkpoint.get("source_key"),
        "resumed": checkpoint.get("source_key") is not None,
        "max_findings": max_findings,
        "findings_examined": findings_examined,
        "findings_stored": findings_stored,
        "total_known_findings": total_known_findings,
        "persisted_findings_total": persisted_findings_total,
        "finding_count": total_known_findings,
        "by_type": dict(sorted(counts.items())),
        "by_severity": dict(sorted(severity.items())),
        "examples": candidates[:50],
        "notes": [
            "Findings are candidates and do not modify stored observations.",
            "Weekday gaps are not exchange-calendar confirmation; holidays, suspensions, and listings require review.",
            "Evidence contains normalized local values only; credentials and raw provider payloads are excluded.",
            "Partial scans resume from a phase and source key; total_known_findings is exact for completed scans or a previously completed source revision.",
        ],
    }


def list_findings(
    path: Path,
    *,
    finding_type: str | None = None,
    code: str | None = None,
    provider: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if finding_type is not None and finding_type not in FINDING_TYPES:
        raise ValueError("Unknown finding type")
    if review_state is not None and review_state not in REVIEW_STATES:
        raise ValueError("Unknown review state")
    if not 1 <= limit <= 10_000 or offset < 0:
        raise ValueError("Invalid finding pagination")
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_quality_findings'"
            ).fetchone()
            is None
        ):
            return {
                "total": 0,
                "limit": limit,
                "offset": offset,
                "returned_count": 0,
                "truncated": False,
                "items": [],
            }
        clauses = []
        params: list[object] = []
        for column, value in (
            ("finding_type", finding_type),
            ("code", code),
            ("provider", provider),
            ("review_state", review_state),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = conn.execute(
            "SELECT COUNT(*) FROM data_quality_findings" + where, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, instrument_kind, code, series_id, provider, session_date, finding_type, "
            "severity, confidence, rule, evidence_json, fingerprint, review_state, "
            "resolution_note, created_at, updated_at FROM data_quality_findings"
            + where
            + " ORDER BY session_date IS NULL, session_date, id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            items.append(item)
        returned_count = len(items)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "returned_count": returned_count,
            "truncated": offset + returned_count < total,
            "items": items,
        }
    finally:
        conn.close()


@serialized_storage_write
def review_finding(path: Path, finding_id: int, to_state: str, note: str | None = None) -> dict:
    if to_state not in REVIEW_STATES:
        raise ValueError("Unknown review state")
    if note is not None and len(note) > 2000:
        raise ValueError("Review note is too long")
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT id, review_state FROM data_quality_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Finding not found")
        now = _iso_now()
        conn.execute(
            "INSERT INTO data_quality_reviews(finding_id, from_state, to_state, note, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (finding_id, row["review_state"], to_state, note, now),
        )
        conn.execute(
            "UPDATE data_quality_findings SET review_state = ?, resolution_note = ?, updated_at = ? "
            "WHERE id = ?",
            (to_state, note, now, finding_id),
        )
        conn.commit()
    except (sqlite3.Error, KeyError):
        conn.rollback()
        raise
    finally:
        conn.close()
    return _get_finding(path, finding_id)


def _get_finding(path: Path, finding_id: int) -> dict:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        row = conn.execute(
            "SELECT id, instrument_kind, code, series_id, provider, session_date, finding_type, "
            "severity, confidence, rule, evidence_json, fingerprint, review_state, "
            "resolution_note, created_at, updated_at FROM data_quality_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Finding not found")
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item
    finally:
        conn.close()


def quality_report(path: Path, *, limit: int = 500) -> dict:
    findings = list_findings(path, limit=limit)
    persisted_findings_total, counts = _stored_finding_summary(path)
    scan_run = _latest_scan_run(path)
    progress = _scan_progress(path)
    current_source_data_version = _source_data_version(path)
    scan_metadata_available = (
        scan_run is not None
        and progress is not None
        and (
            progress["stale"]
            or (scan_run is not None and progress["last_run_key"] == scan_run["run_key"])
        )
    )
    pagination_truncated = bool(findings["truncated"])
    scan_truncated = bool(scan_run["truncated"]) if scan_metadata_available else False
    source_revision_changed = (
        scan_metadata_available
        and current_source_data_version is not None
        and progress["source_data_version_end"] is not None
        and current_source_data_version != progress["source_data_version_end"]
    )
    scan_stale = (
        bool(progress["stale"]) or source_revision_changed if scan_metadata_available else False
    )
    scan_complete = (
        scan_metadata_available
        and not scan_truncated
        and not scan_stale
        and progress["status"] == "completed"
    )
    scan_status = (
        "stale" if scan_stale else scan_run["status"] if scan_metadata_available else "unknown"
    )
    complete = scan_complete and not pagination_truncated
    status = (
        "unknown" if not scan_metadata_available else ("completed" if complete else scan_status)
    )
    total_known_findings = (
        int(scan_run["total_known_findings"]) if scan_run is not None else persisted_findings_total
    )
    findings_examined = int(scan_run["findings_examined"]) if scan_run is not None else None
    findings_stored = (
        int(scan_run["findings_stored"]) if scan_run is not None else persisted_findings_total
    )
    return {
        "generated_at": _iso_now(),
        "status": status,
        "complete": complete,
        "stale": scan_stale,
        "truncated": pagination_truncated,
        "findings_examined": findings_examined,
        "findings_stored": findings_stored,
        "total_known_findings": total_known_findings,
        "persisted_findings_total": persisted_findings_total,
        "source_data_version_current": current_source_data_version,
        "finding_count": total_known_findings,
        "total_returned": len(findings["items"]),
        "returned_count": len(findings["items"]),
        "limit": limit,
        "scan_completeness": (
            {
                "scan_id": scan_run["id"],
                "status": scan_status,
                "complete": scan_complete,
                "truncated": scan_truncated,
                "stale": scan_stale,
                "source_revision_changed": source_revision_changed,
                "max_findings": scan_run["max_findings"],
                "findings_examined": scan_run["findings_examined"],
                "findings_stored": scan_run["findings_stored"],
                "total_known_findings": scan_run["total_known_findings"],
                "persisted_findings_total": scan_run["persisted_findings_total"],
                "source_data_version_start": progress["source_data_version_start"],
                "source_data_version_end": progress["source_data_version_end"],
                "source_data_version_current": current_source_data_version,
                "cursor_start": progress["cursor_start"],
                "cursor_next": progress["cursor_next"],
                "cursor_phase": progress["cursor_phase"],
                "cursor_source_key": (
                    json.loads(progress["cursor_source_key"])
                    if progress["cursor_source_key"]
                    else None
                ),
            }
            if scan_metadata_available
            else None
        ),
        "pagination": {
            "limit": limit,
            "returned_count": len(findings["items"]),
            "total": findings["total"],
            "truncated": pagination_truncated,
        },
        "review_states": dict(sorted(counts.items())),
        "findings": findings["items"],
        "notes": [
            "This report is redacted and local-only; it contains no credentials or raw provider payloads.",
            "A candidate is not a confirmed defect or an automatic correction.",
        ],
    }
