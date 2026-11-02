#!/usr/bin/env python3
"""Issue #29 acceptance evidence on disposable fixtures or explicit read-only data.

Synthetic acceptance is NOT production acceptance. No provider calls, credentials,
portfolio content, or raw historical price samples enter the exported report.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from finintel.market_data.corrections import (
    preview_correction,
    propose_correction,
    resolved_close,
    transition_correction,
)
from finintel.market_data.quality import (
    list_findings,
    quality_overview,
    review_finding,
    run_quality_scan,
)
from finintel.storage.backup import create_backup, restore_backup
from finintel.storage.db import SCHEMA_VERSION, _write_connection

REFERENCE_DATE = date(2026, 11, 1)
MAX_SYNTHETIC_SESSIONS = 20_000
DEFAULT_SESSIONS = 128
PERFORMANCE_BOUND_SECONDS = 30.0


def _check(identifier: str, passed: bool, evidence: dict[str, object]) -> dict:
    return {"id": identifier, "status": "pass" if passed else "fail", "evidence": evidence}


def _sessions(count: int) -> list[str]:
    current = date(2025, 1, 2)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _seed(path: Path, sessions: list[str]) -> None:
    """Deterministic fictitious observations, never a live market download."""
    conn = _write_connection(path)
    with conn:
        conn.execute("INSERT INTO instruments VALUES ('AAPL','2025-01-01')")
        for sid, provider in ((1, "alpaca_sip"), (2, "tiingo")):
            conn.execute(
                "INSERT INTO series VALUES (?,?,?,'1d','raw_unadjusted','historical_eod')",
                (sid, "AAPL", provider),
            )
            conn.execute(
                "INSERT INTO ingestion_runs "
                "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
                "provider_terms_url,first_session,last_session,observations,"
                "inserted,updated,unchanged,stale) "
                "VALUES (?,?,'2026-10-31T00:00:00+00:00',"
                "'2026-10-31T00:00:00+00:00',?,'https://example.test/synthetic',"
                "'https://example.test/terms',?,?,?, ?,0,0,0)",
                (sid, sid, f"synthetic-{sid}", sessions[0], sessions[-1], len(sessions),
                 len(sessions)),
            )
            prices = []
            for index, day in enumerate(sessions):
                close = "250" if sid == 1 and index == 10 else ("101" if sid == 2 else "100")
                prices.append((
                    sid, day, "99", "251" if sid == 1 and index == 10 else "102",
                    "98", close, 1000, "2026-10-31T00:00:00+00:00", sid,
                ))
            conn.executemany(
                "INSERT INTO daily_prices "
                "(series_id,session_date,open,high,low,close,volume,retrieved_at,ingestion_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)", prices,
            )
        conn.executemany(
            "INSERT INTO index_daily_closes "
            "(index_code,provider,session_date,close,retrieved_at,source_url) "
            "VALUES ('SP500','fred',?,?,'2026-10-31T00:00:00+00:00',"
            "'https://fred.stlouisfed.org/series/SP500')",
            [(day, "5000") for day in sessions],
        )
    conn.close()


def _original_snapshot(path: Path) -> tuple[tuple, tuple]:
    with sqlite3.connect(path) as conn:
        return (
            tuple(conn.execute(
                "SELECT series_id,session_date,open,high,low,close,volume,retrieved_at,"
                "ingestion_id FROM daily_prices ORDER BY series_id,session_date"
            )),
            tuple(conn.execute(
                "SELECT index_code,provider,session_date,close,retrieved_at,source_url "
                "FROM index_daily_closes ORDER BY index_code,provider,session_date"
            )),
        )


def _resolved(path: Path, day: str) -> dict:
    return resolved_close(
        path, kind="equity", code="AAPL", provider="alpaca_sip",
        adjustment="raw_unadjusted", frequency="1d", session_date=day,
    )


def _synthetic(path: Path, destination: Path, sessions_count: int) -> tuple[list[dict], dict]:
    days = _sessions(sessions_count)
    _seed(path, days)
    before = _original_snapshot(path)
    checks = []
    started = time.monotonic()
    first = run_quality_scan(path, max_findings=10, as_of=REFERENCE_DATE)
    result = first
    resumes = 0
    while not result["complete"] and resumes < 500:
        result = run_quality_scan(path, max_findings=1000, as_of=REFERENCE_DATE)
        resumes += 1
    duration = round(time.monotonic() - started, 3)
    checks.append(_check(
        "scan.resumable_and_complete",
        first["truncated"] and result["complete"] and resumes > 0,
        {"initial_status": first["status"], "final_status": result["status"],
         "resumed_batches": resumes, "elapsed_seconds": duration,
         "findings_examined": result["findings_examined"]},
    ))
    checks.append(_check(
        "source.original_rows_immutable_after_scan",
        before == _original_snapshot(path),
        {"price_rows": 2 * sessions_count, "index_rows": sessions_count},
    ))
    overview = quality_overview(path, code="SP500", as_of=REFERENCE_DATE)
    checks.append(_check(
        "ui.index_first_read_scope",
        overview["code"] == "SP500"
        and overview["counts"]["instrument_kind"].get("equity", 0) == 0
        and overview["scan"]["complete"],
        {"index": "SP500", "active_findings": overview["total"]},
    ))
    selected = next(
        (item for item in list_findings(path, code="AAPL", limit=500)["items"]
         if item["provider"] == "alpaca_sip"
         and item["session_date"] == days[10]
         and item["finding_type"] == "price_anomaly"),
        None,
    )
    if selected is None:
        checks.append(_check("review.traceable_finding", False, {"reason": "no matching scan finding"}))
        return checks, {"scan_elapsed_seconds": duration, "sessions": sessions_count}

    review_finding(path, selected["id"], "confirmed", "Synthetic independent review")
    proposal = propose_correction(
        path, finding_id=selected["id"], provider="alpaca_sip",
        proposed_close="125", rationale="Synthetic acceptance correction",
    )
    preview = preview_correction(path, proposal["id"])
    transition_correction(path, proposal["id"], "validate")
    transition_correction(path, proposal["id"], "approve", "Synthetic acceptance approved")
    current = _resolved(path, days[10])
    checks.append(_check(
        "review.explicit_overlay_only",
        preview["eligible_for_validation"] and not preview["modifies_stored_prices"]
        and current["overlay_applied"] and current["effective_close"] == "125"
        and current["source"]["close"] == "250"
        and before == _original_snapshot(path),
        {"finding_state": "confirmed", "approved_overlay": True,
         "source_row_preserved": before == _original_snapshot(path)},
    ))

    backup = create_backup(db=path)
    target_connection = _write_connection(destination)
    target_connection.close()
    merged = restore_backup(str(backup["id"]), "merge", db=destination)
    merged_effective = _resolved(destination, days[10])
    checks.append(_check(
        "backup.v15_merge_preserves_approved_audit",
        merged["status"] == "restored" and merged_effective["overlay_applied"]
        and merged_effective["effective_close"] == "125"
        and _original_snapshot(destination) == before,
        {"mode": "merge", "approved_correction_recovered": merged_effective["overlay_applied"]},
    ))
    transition_correction(path, proposal["id"], "reverse", "Synthetic acceptance reversal")
    reversed_source = _resolved(path, days[10])
    checks.append(_check(
        "correction.reversal_preserves_source_and_other_sessions",
        reversed_source["effective_close"] == "250"
        and not reversed_source["overlay_applied"]
        and before == _original_snapshot(path),
        {"state": "reversed", "source_close": "250", "raw_rows_untouched": True},
    ))
    checks.append(_check(
        "performance.synthetic_bound",
        duration <= PERFORMANCE_BOUND_SECONDS if sessions_count <= DEFAULT_SESSIONS else True,
        {"elapsed_seconds": duration, "session_count": sessions_count,
         "threshold_seconds": PERFORMANCE_BOUND_SECONDS if sessions_count <= DEFAULT_SESSIONS else None,
         "larger_fixture_is_observational": sessions_count > DEFAULT_SESSIONS},
    ))
    return checks, {
        "scan_elapsed_seconds": duration,
        "sessions": sessions_count,
        "source_rows": sessions_count * 3,
        "source_database_is_disposable": True,
    }


def _inspect(path: Path) -> tuple[list[dict], dict]:
    """Read-only summary only; never invoke schema upgrades, scans, providers or backups."""
    if not path.is_file() or path.is_symlink():
        raise ValueError("Inspection requires an existing regular SQLite database")
    start = time.monotonic()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        required = {
            "daily_prices", "index_daily_closes", "data_quality_findings",
            "data_quality_reviews", "quality_corrections", "quality_correction_events",
        }
        has_required = required.issubset(tables)
        counts = {}
        for table in ("daily_prices", "index_daily_closes", "data_quality_findings",
                      "quality_corrections", "quality_correction_events"):
            if table in tables:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # This query is scoped to the index and bounded; no actual price row leaves SQLite.
        if "data_quality_findings" in tables:
            count = conn.execute(
                "SELECT COUNT(*) FROM data_quality_findings "
                "WHERE code='SP500' AND superseded_at IS NULL"
            ).fetchone()[0]
            plan = [
                row[3] for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM data_quality_findings "
                    "WHERE code='SP500' AND superseded_at IS NULL LIMIT 20"
                )
            ]
        else:
            count, plan = 0, []
    elapsed = round(time.monotonic() - start, 3)
    checks = [
        _check("local.read_only_schema", version == SCHEMA_VERSION and has_required,
               {"schema_version": version, "expected": SCHEMA_VERSION,
                "quality_tables_present": has_required}),
        _check("local.index_scoped_query", bool(plan),
               {"scope": "SP500", "active_findings": count, "elapsed_seconds": elapsed}),
    ]
    return checks, {"counts": counts, "elapsed_seconds": elapsed,
                    "large_dataset_observed": counts.get("daily_prices", 0) >= 1_000_000,
                    "inspection_only": True, "price_samples_exported": False}


def _report(checks: list[dict], synthetic: dict, inspection: dict | None) -> dict:
    synthetic_pass = all(item["status"] == "pass" for item in checks)
    manual = {
        name: {"status": "not_run", "reason": "Requires actual installed desktop testing"}
        for name in ("macos", "windows", "linux")
    }
    return {
        "format": "finintel-issue-29-acceptance-v1",
        "synthetic": {"status": "pass" if synthetic_pass else "fail", **synthetic},
        "read_only_local_inspection": inspection or {"status": "not_run"},
        "manual_desktop": manual,
        "optional_live_providers": {"status": "not_run", "reason": "No credentials or network calls"},
        "large_production_performance": {
            "status": "observed" if inspection and inspection["large_dataset_observed"]
            else "not_run",
            "note": "Read-only local metadata inspection is not a full production load test",
        },
        "checks": checks,
        "verdict": "synthetic_pass_production_unverified" if synthetic_pass
        else "synthetic_failure",
        "notes": [
            "Generated from fabricated synthetic market observations.",
            "No credentials, provider payloads, portfolios or price samples are in this report.",
            "A green CI check does not establish manual desktop or live provider acceptance.",
            "Never run a correction scan against a production database from this harness.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Redacted JSON output file")
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--inspect-db", type=Path, help="Optional existing local database (read-only)")
    args = parser.parse_args(argv)
    if not 32 <= args.sessions <= MAX_SYNTHETIC_SESSIONS:
        parser.error(f"--sessions must be in [32, {MAX_SYNTHETIC_SESSIONS}]")
    output = args.output.expanduser().absolute()
    if args.inspect_db:
        user_db = args.inspect_db.expanduser().absolute()
        if output == user_db or output in {
            Path(str(user_db) + suffix) for suffix in ("-wal", "-shm", "-journal")
        }:
            parser.error("Report output may not overwrite the inspected database or its journals")
    if output.exists():
        parser.error("Report output already exists; choose a new filename to avoid overwrite")
    if not output.parent.is_dir():
        parser.error("Report directory does not exist")
    with tempfile.TemporaryDirectory(prefix="finintel-quality-acceptance-") as directory:
        root = Path(directory)
        checks, synthetic = _synthetic(
            root / "synthetic.sqlite3", root / "destination.sqlite3", args.sessions
        )
    inspection = None
    if args.inspect_db:
        local_checks, inspection = _inspect(user_db)
        checks += local_checks
    report = _report(checks, synthetic, inspection)
    # No user database is ever opened for writing. Output is a new local file.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"Issue #29 synthetic: {report['synthetic']['status']}; production: unverified")
    return 0 if all(item["status"] == "pass" for item in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
