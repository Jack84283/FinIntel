"""Durable, local-first historical data-quality candidates.

The scanner never changes market observations.  It records deterministic
candidate findings with redacted evidence, and review actions are append-only
history plus a current review state.
"""

import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import groupby
from pathlib import Path

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


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(
    *,
    code: str,
    provider: str,
    session_date: str | None,
    finding_type: str,
    rule: str,
) -> str:
    value = "|".join((code, provider, session_date or "", finding_type, rule))
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
        "fingerprint": _fingerprint(
            code=code,
            provider=provider,
            session_date=session_date,
            finding_type=finding_type,
            rule=rule,
        ),
    }


def _decimal(value: object) -> object:
    from decimal import Decimal, InvalidOperation

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _read_candidates(path: Path, max_findings: int) -> list[dict]:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    rows = conn.execute(
        "SELECT s.id AS series_id, s.symbol, s.provider, s.adjustment, "
        "dp.session_date, dp.open, dp.high, dp.low, dp.close, dp.volume "
        "FROM series AS s JOIN daily_prices AS dp ON dp.series_id = s.id "
        "ORDER BY s.id, dp.session_date"
    )
    index_rows = conn.execute(
        "SELECT index_code AS symbol, provider, session_date, close "
        "FROM index_daily_closes ORDER BY index_code, provider, session_date"
    )

    # These cursors are consumed in indexed order. Only one series (and later
    # one overlapping provider/session group) is retained at a time; the full
    # price database is never materialized in memory.
    findings: list[dict] = []

    def add(item: dict) -> bool:
        if len(findings) >= max_findings:
            return False
        findings.append(item)
        return True

    for series_id, series_rows in groupby(rows, key=lambda row: row["series_id"]):
        previous: tuple[date, object, str] | None = None
        for row in series_rows:
            session_date = row["session_date"]
            try:
                current_date = date.fromisoformat(session_date)
            except (TypeError, ValueError):
                add(
                    _candidate(
                        series_id=series_id,
                        code=row["symbol"],
                        provider=row["provider"],
                        session_date=session_date,
                        finding_type="ohlcv_anomaly",
                        severity="error",
                        confidence="high",
                        rule="invalid_session_date",
                        evidence={"session_date": str(session_date)},
                    )
                )
                continue

            values = {name: _decimal(row[name]) for name in ("open", "high", "low", "close")}
            volume = row["volume"]
            invalid = [name for name, value in values.items() if value is None]
            try:
                volume_value = int(volume)
            except (TypeError, ValueError):
                volume_value = None
            if volume_value is None or volume_value < 0:
                invalid.append("volume")
            if not invalid and not (
                values["low"] <= values["open"] <= values["high"]
                and values["low"] <= values["close"] <= values["high"]
            ):
                invalid.append("ohlc_order")
            if invalid:
                add(
                    _candidate(
                        series_id=series_id,
                        code=row["symbol"],
                        provider=row["provider"],
                        session_date=session_date,
                        finding_type="ohlcv_anomaly",
                        severity="error",
                        confidence="high",
                        rule="validated_ohlcv_constraints",
                        evidence={"invalid_fields": invalid},
                    )
                )

            close = values["close"]
            if (
                previous
                and close is not None
                and close > 0
                and previous[1] is not None
                and previous[1] > 0
            ):
                ratio = close / previous[1]
                if ratio >= 2 or ratio <= 0.5:
                    add(
                        _candidate(
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
                            session_date=session_date,
                            finding_type="price_anomaly",
                            severity="warning",
                            confidence="low",
                            rule="close_jump_over_2x_or_under_half",
                            evidence={
                                "previous_session": previous[0].isoformat(),
                                "previous_close": str(previous[1]),
                                "close": str(close),
                                "ratio": str(ratio),
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
                            series_id=series_id,
                            code=row["symbol"],
                            provider=row["provider"],
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
            if close is not None:
                previous = (current_date, close, row["session_date"])

    for (index_code, provider), index_series in groupby(
        index_rows, key=lambda row: (row["symbol"], row["provider"])
    ):
        previous: tuple[date, object] | None = None
        for row in index_series:
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
            if previous and close is not None and close > 0 and previous[1] > 0:
                ratio = close / previous[1]
                if ratio >= 2 or ratio <= 0.5:
                    add(
                        _candidate(
                            instrument_kind="index",
                            series_id=None,
                            code=index_code,
                            provider=provider,
                            session_date=session_date,
                            finding_type="price_anomaly",
                            severity="warning",
                            confidence="low",
                            rule="close_jump_over_2x_or_under_half",
                            evidence={
                                "previous_session": previous[0].isoformat(),
                                "previous_close": str(previous[1]),
                                "close": str(close),
                                "ratio": str(ratio),
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

    provider_rows = conn.execute(
        "SELECT s.id AS series_id, s.symbol, s.provider, s.adjustment, "
        "dp.session_date, dp.close FROM series AS s "
        "JOIN daily_prices AS dp ON dp.series_id = s.id "
        "ORDER BY s.symbol, s.adjustment, dp.session_date, s.provider, s.id"
    )
    for (symbol, adjustment, session), observation_group in groupby(
        provider_rows, key=lambda row: (row["symbol"], row["adjustment"], row["session_date"])
    ):
        observations = list(observation_group)
        if len(observations) < 2:
            continue
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
    conn.close()
    return findings


def _persist(path: Path, candidates: list[dict]) -> None:
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN")
        now = _iso_now()
        for item in candidates:
            conn.execute(
                "INSERT INTO data_quality_findings "
                "(instrument_kind, code, series_id, provider, session_date, finding_type, "
                "severity, confidence, rule, evidence_json, fingerprint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
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
def run_quality_scan(path: Path, *, max_findings: int = 1000) -> dict:
    """Scan local observations and persist candidate findings without repairing data."""

    if not 1 <= max_findings <= 10_000:
        raise ValueError("max_findings must be between 1 and 10000")
    if not path.is_file():
        raise FileNotFoundError(path)
    # Quality scans may be the first write after an older database is opened.
    # Migrate before reading so legacy v1-v13 databases have all required tables.
    connection = _write_connection(path)
    connection.close()
    candidates = _read_candidates(path, max_findings)
    _persist(path, candidates)
    counts = Counter(item["finding_type"] for item in candidates)
    severity = Counter(item["severity"] for item in candidates)
    return {
        "status": "completed",
        "finding_count": len(candidates),
        "by_type": dict(sorted(counts.items())),
        "by_severity": dict(sorted(severity.items())),
        "examples": candidates[:50],
        "notes": [
            "Findings are candidates and do not modify stored observations.",
            "Weekday gaps are not exchange-calendar confirmation; holidays, suspensions, and listings require review.",
            "Evidence contains normalized local values only; credentials and raw provider payloads are excluded.",
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
    if not 1 <= limit <= 500 or offset < 0:
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
            return {"total": 0, "limit": limit, "offset": offset, "items": []}
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
        return {"total": total, "limit": limit, "offset": offset, "items": items}
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
    counts = Counter(item["review_state"] for item in findings["items"])
    return {
        "generated_at": _iso_now(),
        "status": "candidate_findings",
        "total_returned": len(findings["items"]),
        "review_states": dict(sorted(counts.items())),
        "findings": findings["items"],
        "notes": [
            "This report is redacted and local-only; it contains no credentials or raw provider payloads.",
            "A candidate is not a confirmed defect or an automatic correction.",
        ],
    }
