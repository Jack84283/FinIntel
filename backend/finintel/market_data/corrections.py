"""Explicit, reversible market-data corrections; never rewrite source observations.

An approval records a locally authorized *overlay*. Existing market APIs still
return original data; consumers must deliberately request the resolved view.
All review transitions append immutable events and check current source revision.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from finintel.market_data.corporate_reference import verify_events
from finintel.storage.db import _read_connection, _write_connection, serialized_storage_write

STATES = ("proposed", "validated", "approved", "rejected", "reversed")
KINDS = ("equity", "index")


class CorrectionConflict(ValueError):
    """Concurrent source revisions or an invalid correction state transition."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _positive(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("Correction close must be a finite positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("Correction close must be a finite positive decimal")
    return parsed


def _source(
    conn: sqlite3.Connection,
    *,
    kind: str,
    code: str,
    provider: str,
    adjustment: str,
    frequency: str,
    session: str,
) -> dict:
    """Resolve by stable natural key, never by database-local series/ingestion ID."""

    if kind == "equity":
        if adjustment != "raw_unadjusted" or frequency != "1d":
            raise ValueError("Only daily raw_unadjusted equity price corrections are supported")
        row = conn.execute(
            "SELECT p.close, p.retrieved_at FROM series s "
            "JOIN daily_prices p ON p.series_id=s.id "
            "WHERE s.symbol=? AND s.provider=? AND s.frequency=? "
            "AND s.adjustment=? AND p.session_date=?",
            (code, provider, frequency, adjustment, session),
        ).fetchone()
        semantics = "raw_unadjusted_equity_price_usd_per_share"
    elif kind == "index":
        if frequency != "daily":
            raise ValueError("Index corrections require daily frequency")
        meta = conn.execute(
            "SELECT series_type,currency,units FROM index_series_catalog "
            "WHERE index_code=? AND provider=?",
            (code, provider),
        ).fetchone()
        if meta is None or meta["units"] != "index_points":
            raise ValueError("Index series semantic metadata is unavailable")
        semantics = "volatility_index" if meta["series_type"] == "volatility" else "price_index"
        if adjustment != semantics:
            raise ValueError("Index series adjustment does not match its recorded return semantics")
        row = conn.execute(
            "SELECT close,retrieved_at FROM index_daily_closes "
            "WHERE index_code=? AND provider=? AND session_date=?",
            (code, provider, session),
        ).fetchone()
    else:
        raise ValueError("Unsupported instrument kind")
    if row is None:
        raise KeyError("Source observation does not exist")
    _positive(row["close"])
    return {
        "close": str(row["close"]),
        "retrieved_at": row["retrieved_at"],
        "return_semantics": semantics,
        "kind": kind,
        "code": code,
        "provider": provider,
        "adjustment": adjustment,
        "frequency": frequency,
        "session_date": session,
    }


def _read_correction(conn: sqlite3.Connection, correction_id: str) -> dict:
    row = conn.execute("SELECT * FROM quality_corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise KeyError("Correction proposal was not found")
    result = dict(row)
    result["evidence"] = json.loads(result.pop("evidence_json"))
    result["events"] = [
        dict(item)
        for item in conn.execute(
            "SELECT id, from_state, to_state, note, occurred_at "
            "FROM quality_correction_events WHERE correction_id=? "
            "ORDER BY occurred_at, id",
            (correction_id,),
        )
    ]
    return result


def get_correction(path: Path, correction_id: str) -> dict:
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        return _read_correction(conn, correction_id)
    finally:
        conn.close()


def list_corrections(
    path: Path,
    *,
    code: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if state is not None and state not in STATES:
        raise ValueError("Invalid correction state")
    if q is not None and (len(q) > 80 or any(ord(c) < 32 for c in q)):
        raise ValueError("Invalid correction search")
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("Invalid pagination")
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        clauses = []
        values = []
        if code is not None:
            clauses.append("code=?")
            values.append(code)
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        search = (q or "").strip()
        if search:
            # Literal substring matching, not SQL wildcards or interpolated SQL.
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            clauses.append(
                "("
                + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\'"
                    for column in (
                        "session_date",
                        "provider",
                        "state",
                        "source_close",
                        "proposed_close",
                    )
                )
                + ")"
            )
            values.extend([pattern] * 5)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = conn.execute("SELECT COUNT(*) FROM quality_corrections" + where, values).fetchone()[
            0
        ]
        rows = conn.execute(
            "SELECT id FROM quality_corrections"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_read_correction(conn, row["id"]) for row in rows],
        }
    finally:
        conn.close()


def _record_event(
    conn: sqlite3.Connection,
    correction_id: str,
    old: str | None,
    new: str,
    note: str | None,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO quality_correction_events "
        "(id,correction_id,from_state,to_state,note,occurred_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(uuid4()), correction_id, old, new, note, now),
    )


@serialized_storage_write
def propose_correction(
    path: Path, *, finding_id: int, provider: str, proposed_close: str, rationale: str
) -> dict:
    """Require an explicitly confirmed, currently active quality finding."""

    proposed = _positive(proposed_close)
    if not 1 <= len(rationale.strip()) <= 2000:
        raise ValueError("Correction rationale is required (max 2000 characters)")
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        finding = conn.execute(
            "SELECT instrument_kind,code,series_id,provider,session_date,"
            "fingerprint,review_state,superseded_at FROM data_quality_findings WHERE id=?",
            (finding_id,),
        ).fetchone()
        if finding is None:
            raise KeyError("Quality finding was not found")
        if finding["review_state"] != "confirmed" or finding["superseded_at"] is not None:
            raise CorrectionConflict(
                "A confirmed, active finding is required before proposing a correction"
            )
        if finding["session_date"] is None:
            raise ValueError("Finding does not identify a specific observed session")
        kind, code = finding["instrument_kind"], finding["code"]
        if kind == "equity":
            if provider not in finding["provider"].split("|"):
                raise ValueError("Provider is not part of the confirmed finding")
            candidate = conn.execute(
                "SELECT frequency,adjustment FROM series WHERE symbol=? AND provider=? "
                "AND frequency='1d' AND adjustment='raw_unadjusted'",
                (code, provider),
            ).fetchone()
            if candidate is None:
                raise ValueError("No supported raw equity source for this provider")
            adjustment, frequency = candidate["adjustment"], candidate["frequency"]
        else:
            if provider != finding["provider"]:
                raise ValueError("Provider is not part of the confirmed index finding")
            meta = conn.execute(
                "SELECT series_type FROM index_series_catalog WHERE index_code=? AND provider=?",
                (code, provider),
            ).fetchone()
            if meta is None:
                raise ValueError("Index source semantics are unknown")
            adjustment = (
                "volatility_index" if meta["series_type"] == "volatility" else "price_index"
            )
            frequency = "daily"
        source = _source(
            conn,
            kind=kind,
            code=code,
            provider=provider,
            adjustment=adjustment,
            frequency=frequency,
            session=finding["session_date"],
        )
        if proposed == _positive(source["close"]):
            raise ValueError("Proposed close must differ from the recorded observation")
        now, key = _now(), str(uuid4())
        conn.execute(
            "INSERT INTO quality_corrections "
            "(id,instrument_kind,code,provider,adjustment,frequency,return_semantics,"
            "session_date,source_close,source_retrieved_at,proposed_close,"
            "finding_fingerprint,rationale,evidence_json,state,validated_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',NULL,?,?)",
            (
                key,
                kind,
                code,
                provider,
                adjustment,
                frequency,
                source["return_semantics"],
                finding["session_date"],
                source["close"],
                source["retrieved_at"],
                str(proposed),
                finding["fingerprint"],
                rationale.strip(),
                json.dumps(
                    {"candidate_only": True, "source_semantics": source["return_semantics"]},
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        _record_event(conn, key, None, "proposed", rationale.strip(), now)
        conn.commit()
        return get_correction(path, key)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _status(conn: sqlite3.Connection, proposal: dict) -> tuple[dict, bool]:
    try:
        current = _source(
            conn,
            kind=proposal["instrument_kind"],
            code=proposal["code"],
            provider=proposal["provider"],
            adjustment=proposal["adjustment"],
            frequency=proposal["frequency"],
            session=proposal["session_date"],
        )
    except (KeyError, ValueError):
        return {}, True
    stale = (
        str(current["close"]) != str(proposal["source_close"])
        or current["retrieved_at"] != proposal["source_retrieved_at"]
        or current["return_semantics"] != proposal["return_semantics"]
    )
    return current, stale


def preview_correction(path: Path, correction_id: str) -> dict:
    """Read-only hypothetical overlay, with stale-revision and collision flags."""

    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        p = _read_correction(conn, correction_id)
        current, stale = _status(conn, p)
        collision = conn.execute(
            "SELECT id FROM quality_corrections WHERE instrument_kind=? AND code=? "
            "AND provider=? AND adjustment=? AND frequency=? AND session_date=? "
            "AND state='approved' AND id<>?",
            (
                p["instrument_kind"],
                p["code"],
                p["provider"],
                p["adjustment"],
                p["frequency"],
                p["session_date"],
                p["id"],
            ),
        ).fetchone()
        proposed, original = _positive(p["proposed_close"]), _positive(p["source_close"])
        return {
            "correction_id": p["id"],
            "state": p["state"],
            "source": current or None,
            "captured_source_close": str(original),
            "proposed_close": str(proposed),
            "absolute_delta": str(proposed - original),
            "relative_delta": str((proposed - original) / original),
            "stale_source": stale,
            "approved_conflict": collision["id"] if collision else None,
            "eligible_for_validation": p["state"] == "proposed" and not stale and collision is None,
            "eligible_for_approval": p["state"] == "validated" and not stale and collision is None,
            "candidate_only": True,
            "modifies_stored_prices": False,
        }
    finally:
        conn.close()


@serialized_storage_write
def transition_correction(
    path: Path, correction_id: str, action: str, note: str | None = None
) -> dict:
    """Atomically validate, approve, reject or reverse, recording every transition."""

    targets = {
        "validate": ("proposed", "validated"),
        "approve": ("validated", "approved"),
        "reject": (("proposed", "validated"), "rejected"),
        "reverse": ("approved", "reversed"),
    }
    if action not in targets:
        raise ValueError("Unknown correction action")
    if note is not None and len(note) > 2000:
        raise ValueError("Correction note is too long")
    if action in ("approve", "reverse", "reject") and not (note or "").strip():
        raise ValueError("An approval, rejection or reversal note is required")
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        p = _read_correction(conn, correction_id)
        allowed, target = targets[action]
        if p["state"] == target:
            conn.commit()
            return get_correction(path, correction_id)
        if p["state"] not in (allowed if isinstance(allowed, tuple) else (allowed,)):
            raise CorrectionConflict(f"Cannot {action} a correction in state {p['state']}")
        if action in ("validate", "approve"):
            _, stale = _status(conn, p)
            if stale:
                raise CorrectionConflict(
                    "Source observation or semantics changed; propose a new correction"
                )
            match = conn.execute(
                "SELECT id FROM quality_corrections WHERE instrument_kind=? AND code=? "
                "AND provider=? AND adjustment=? AND frequency=? AND session_date=? "
                "AND state='approved' AND id<>?",
                (
                    p["instrument_kind"],
                    p["code"],
                    p["provider"],
                    p["adjustment"],
                    p["frequency"],
                    p["session_date"],
                    p["id"],
                ),
            ).fetchone()
            if match is not None:
                raise CorrectionConflict(
                    "A different approved overlay already exists for this observation"
                )
            # A finding can be superseded after a proposal was created. Approval
            # requires the evidence to remain confirmed and active, not merely a
            # compatible source close.
            finding = conn.execute(
                "SELECT review_state,superseded_at FROM data_quality_findings WHERE fingerprint=?",
                (p["finding_fingerprint"],),
            ).fetchone()
            if (
                finding is None
                or finding["review_state"] != "confirmed"
                or finding["superseded_at"]
            ):
                raise CorrectionConflict(
                    "The linked quality finding is no longer confirmed and active"
                )
        now = _now()
        conn.execute(
            "UPDATE quality_corrections SET state=?,updated_at=?,validated_at="
            "CASE WHEN ?='validated' THEN ? ELSE validated_at END WHERE id=?",
            (target, now, target, now, correction_id),
        )
        _record_event(conn, correction_id, p["state"], target, note, now)
        conn.commit()
        return get_correction(path, correction_id)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CorrectionConflict(
            "Conflicting approved correction or invalid audit transition"
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolved_close(
    path: Path,
    *,
    kind: str,
    code: str,
    provider: str,
    adjustment: str,
    frequency: str,
    session_date: str,
) -> dict:
    """Explicit read-side overlay. A stale approval never masks a provider revision."""

    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        current = _source(
            conn,
            kind=kind,
            code=code,
            provider=provider,
            adjustment=adjustment,
            frequency=frequency,
            session=session_date,
        )
        row = conn.execute(
            "SELECT id,source_close,source_retrieved_at,return_semantics,proposed_close "
            "FROM quality_corrections WHERE instrument_kind=? AND code=? AND provider=? "
            "AND adjustment=? AND frequency=? AND session_date=? AND state='approved'",
            (kind, code, provider, adjustment, frequency, session_date),
        ).fetchone()
        valid = bool(
            row is not None
            and row["source_close"] == current["close"]
            and row["source_retrieved_at"] == current["retrieved_at"]
            and row["return_semantics"] == current["return_semantics"]
        )
        return {
            "source": current,
            "approved_overlay": row["id"] if row else None,
            "effective_close": row["proposed_close"] if valid else current["close"],
            "overlay_applied": valid,
            "stale_overlay": bool(row and not valid),
            "read_only": True,
            "default_historical_apis_unchanged": True,
        }
    finally:
        conn.close()


def record_verified_actions(path: Path, symbol: str, *, client=None) -> dict:
    """Fetch on explicit request without holding the storage writer lock."""

    result = verify_events(path, symbol, client=client)
    return _save_verified_actions(path, result)


@serialized_storage_write
def _save_verified_actions(path: Path, result: dict) -> dict:
    """Persist screened events atomically after all remote I/O completes."""

    conn = _write_connection(path)
    saved = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for event in result["events"]:
            for kind in ("split", "dividend"):
                if kind == "split" and Decimal(event["split_factor"]) == 1:
                    continue
                if kind == "dividend" and Decimal(event["cash_dividend"]) == 0:
                    continue
                observed = _now()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO corporate_action_evidence "
                    "(id,code,provider,session_date,kind,split_factor,cash_dividend,status,"
                    "source_name,source_observed_at,evidence_json,recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        result["symbol"],
                        "tiingo",
                        event["date"],
                        kind,
                        event["split_factor"],
                        event["cash_dividend"],
                        "sourced",
                        "Tiingo daily event fields",
                        observed,
                        json.dumps(
                            {
                                "check": event["check"],
                                "observed_close_ratio": event.get("observed_close_ratio"),
                                "expected_split_ratio": event.get("expected_split_ratio"),
                            },
                            sort_keys=True,
                        ),
                        observed,
                    ),
                )
                saved += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "symbol": result["symbol"],
        "verified_events": result["event_count"],
        "evidence_records": saved,
        "status": "sourced_reference_not_correction",
        "original_prices_modified": False,
    }


def list_corporate_actions(path: Path, code: str, *, limit: int = 100) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError("Invalid limit")
    conn = _read_connection(path)
    if conn is None:
        raise FileNotFoundError(path)
    try:
        sourced = [
            {**dict(row), "evidence": json.loads(row["evidence_json"])}
            for row in conn.execute(
                "SELECT * FROM corporate_action_evidence WHERE code=? "
                "ORDER BY session_date DESC, recorded_at DESC LIMIT ?",
                (code, limit),
            )
        ]
        for item in sourced:
            item.pop("evidence_json")
        suspected = [
            {
                "finding_id": row["id"],
                "session_date": row["session_date"],
                "provider": row["provider"],
                "status": "suspected",
                "rule": row["rule"],
                "review_state": row["review_state"],
            }
            for row in conn.execute(
                "SELECT id,session_date,provider,rule,review_state FROM data_quality_findings "
                "WHERE code=? AND finding_type='price_anomaly' AND rule='price_jump_candidate' "
                "AND superseded_at IS NULL ORDER BY session_date DESC LIMIT ?",
                (code, limit),
            )
        ]
        return {
            "code": code,
            "sourced": sourced,
            "suspected": suspected,
            "note": "A price-jump suspicion is not a sourced corporate action or an approved correction.",
        }
    finally:
        conn.close()
