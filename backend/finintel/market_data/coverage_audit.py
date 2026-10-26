"""Read-only, eligibility-aware historical session coverage audit.

Calendar gaps are candidates, not proof of missing vendor data: halts, venue
differences and licensing can explain them. Nothing here fetches or changes prices.
"""

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import exchange_calendars as xcals

CALENDARS = {"NYSE": "XNYS", "XNYS": "XNYS", "NASDAQ": "XNAS", "XNAS": "XNAS"}
EVIDENCE_FIELDS = {"symbol", "listed_on", "delisted_on", "source", "verified_on"}


def _year_start(through: date, years: int) -> date:
    try:
        return through.replace(year=through.year - years) + timedelta(days=1)
    except ValueError:  # Leap day
        return through.replace(year=through.year - years, day=28) + timedelta(days=1)


def load_listing_evidence(path: Path | None, *, through: date) -> dict[str, dict]:
    """Read sourced listing dates and optional calendar; never infer from OHLCV."""
    if path is None:
        return {}
    evidence = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not EVIDENCE_FIELDS.issubset(reader.fieldnames):
            raise ValueError("Listing evidence CSV needs: " + ", ".join(sorted(EVIDENCE_FIELDS)))
        for line, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            source = (row.get("source") or "").strip()
            if not symbol or not source or symbol in evidence:
                raise ValueError(f"Invalid, unsourced, or duplicate listing evidence at line {line}")
            try:
                listed = date.fromisoformat((row.get("listed_on") or "").strip())
                delisted_raw = (row.get("delisted_on") or "").strip()
                delisted = date.fromisoformat(delisted_raw) if delisted_raw else None
                verified = date.fromisoformat((row.get("verified_on") or "").strip())
            except ValueError as exc:
                raise ValueError(f"Invalid listing evidence date at line {line}") from exc
            if (delisted is not None and delisted < listed) or verified > through:
                raise ValueError(f"Inconsistent listing evidence at line {line}")
            evidence_calendar = (row.get("calendar") or "").strip().upper()
            if evidence_calendar and evidence_calendar not in ("XNYS", "XNAS"):
                raise ValueError(f"Unsupported evidence calendar at line {line}")
            evidence[symbol] = {
                "calendar": evidence_calendar or None,
                "listed_on": listed,
                "delisted_on": delisted,
                "source": source,
                "verified_on": verified,
            }
    return evidence


def _contiguous_ranges(days: list[str], all_sessions: list[str]) -> list[dict]:
    """Group successive exchange sessions, including across holidays/weekends."""
    if not days:
        return []
    positions = {session: index for index, session in enumerate(all_sessions)}
    ranges = []
    first = previous = days[0]
    count = 1
    for day in days[1:]:
        if positions[day] == positions[previous] + 1:
            count += 1
        else:
            ranges.append({"start": first, "end": previous, "sessions": count})
            first, count = day, 1
        previous = day
    ranges.append({"start": first, "end": previous, "sessions": count})
    return ranges


def audit_coverage(
    db: Path,
    *,
    through: date,
    years: int = 10,
    listing_evidence: dict[str, dict] | None = None,
    examples_limit: int = 30,
) -> dict:
    """Audit an existing SQLite DB strictly read-only; no mutations or fetches."""
    if not 1 <= years <= 30 or not 0 <= examples_limit <= 1000:
        raise ValueError("years must be 1..30 and examples_limit must be 0..1000")
    if through > datetime.now(UTC).date():
        raise ValueError("Cannot audit a future as-of date")
    if not db.is_file():
        raise FileNotFoundError(db)
    start = _year_start(through, years)
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        catalog = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbol_catalog'"
            ).fetchone()
            is not None
        )
        columns = "c.asset_type, c.exchange" if catalog else "NULL AS asset_type, NULL AS exchange"
        join = "LEFT JOIN symbol_catalog c ON c.symbol=s.symbol" if catalog else ""
        series = conn.execute(
            f"SELECT s.id, s.symbol, s.provider, s.frequency, s.adjustment, {columns} "
            f"FROM series s {join} ORDER BY s.symbol, s.provider, s.id"
        ).fetchall()

        calendar_sessions = {}
        for name in sorted(set(CALENDARS.values())):
            calendar = xcals.get_calendar(name, start=start.isoformat(), end=through.isoformat())
            calendar_sessions[name] = [session.date().isoformat() for session in calendar.sessions]

        entries = []
        manifest = []
        summary = Counter()
        evidence = listing_evidence or {}
        for item in series:
            symbol, provider = item["symbol"], item["provider"]
            venue = (item["exchange"] or "").upper().strip()
            listing = evidence.get(symbol)
            calendar_name = (listing or {}).get("calendar") or CALENDARS.get(venue)
            rows = conn.execute(
                "SELECT session_date FROM daily_prices "
                "WHERE series_id=? AND session_date>=? AND session_date<=? "
                "ORDER BY session_date",
                (item["id"], start.isoformat(), through.isoformat()),
            )
            observed = [row[0] for row in rows]
            observed_set = set(observed)
            entry = {
                "symbol": symbol,
                "provider": provider,
                "adjustment": item["adjustment"],
                "asset_type": item["asset_type"],
                "exchange": item["exchange"],
                "calendar": calendar_name,
                "observed_sessions": len(observed),
                "first_observed": observed[0] if observed else None,
                "last_observed": observed[-1] if observed else None,
                "eligible_sessions": None,
                "eligible_sessions_present": None,
                "eligible_sessions_present_pct": None,
                "internal_gap_candidates": 0,
                "eligible_gap_candidates": 0,
                "off_calendar_observations": 0,
                "outside_eligibility_observations": 0,
                "candidate_ranges": 0,
                "status": "eligibility_unknown",
                "reasons": [],
            }
            if item["frequency"] != "1d" or item["asset_type"] not in ("Stock", "ETF"):
                entry["status"] = "unsupported_instrument"
                entry["reasons"].append("Only catalogued daily Stock/ETF series are audited")
            elif not calendar_name:
                entry["status"] = "calendar_unresolved"
                entry["reasons"].append("No verified exchange-calendar mapping")
            else:
                sessions = calendar_sessions[calendar_name]
                session_set = set(sessions)
                off_calendar = sum(day not in session_set for day in observed_set)
                entry["off_calendar_observations"] = off_calendar
                summary["off_calendar_observations"] += off_calendar
                if observed:
                    internal = [
                        session
                        for session in sessions
                        if observed[0] < session < observed[-1] and session not in observed_set
                    ]
                    entry["internal_gap_candidates"] = len(internal)
                    summary["internal_gap_candidates"] += len(internal)
                if not listing:
                    entry["reasons"].append(
                        "Listing/delisting dates are unverified; leading/trailing coverage unknown"
                    )
                else:
                    from_day = max(start, listing["listed_on"])
                    to_day = min(through, listing["delisted_on"] or through)
                    eligible = (
                        [
                            session
                            for session in sessions
                            if from_day.isoformat() <= session <= to_day.isoformat()
                        ]
                        if from_day <= to_day
                        else []
                    )
                    eligible_set = set(eligible)
                    present = sum(day in observed_set for day in eligible)
                    missing = [day for day in eligible if day not in observed_set]
                    outside_eligibility = sum(day not in eligible_set for day in observed_set)
                    entry["outside_eligibility_observations"] = outside_eligibility
                    summary["outside_eligibility_observations"] += outside_eligibility
                    entry["eligible_sessions"] = len(eligible)
                    entry["eligible_sessions_present"] = present
                    entry["eligible_sessions_present_pct"] = (
                        round(100 * present / len(eligible), 2) if eligible else None
                    )
                    entry["eligible_gap_candidates"] = len(missing)
                    entry["evidence_source"] = listing["source"]
                    entry["evidence_verified_on"] = listing["verified_on"].isoformat()
                    ranges = _contiguous_ranges(missing, sessions)
                    entry["candidate_ranges"] = len(ranges)
                    for group in ranges:
                        manifest.append(
                            {
                                "symbol": symbol,
                                "provider": provider,
                                "calendar": calendar_name,
                                "adjustment": item["adjustment"],
                                **group,
                                "classification": "needs_manual_review",
                                "safe_to_auto_fetch": False,
                                "reason": "Check trading halts, venue and provider before fetching",
                                "evidence_source": listing["source"],
                            }
                        )
                    summary["eligible_gap_candidates"] += len(missing)
                    if outside_eligibility or off_calendar:
                        entry["status"] = "needs_review"
                        entry["reasons"].append("Unexpected observations outside verified sessions")
                    elif not eligible:
                        entry["status"] = "outside_verified_listing_window"
                    elif listing["verified_on"] < through and listing["delisted_on"] is None:
                        entry["status"] = "stale_listing_evidence"
                        entry["reasons"].append("Active listing evidence predates report through date")
                    elif missing:
                        entry["status"] = "needs_review"
                        entry["reasons"].append("Calendar gaps require halt/provider verification")
                    else:
                        entry["status"] = "calendar_coverage_present"
                        entry["reasons"].append("Does not certify provider accuracy or adjustments")
            summary[entry["status"]] += 1
            entries.append(entry)
        return {
            "schema_version": 1,
            "database": str(db),
            "from": start.isoformat(),
            "through": through.isoformat(),
            "calendar_policy": "Explicit XNYS/XNAS for catalogued daily Stock/ETF only",
            "series_count": len(entries),
            "summary": dict(sorted(summary.items())),
            "series": entries,
            "repair_candidates_count": len(manifest),
            "repair_candidates_examples": manifest[:examples_limit],
            "repair_manifest": manifest,
            "limitations": [
                "Scheduled exchange sessions do not prove an instrument traded or vendor had a bar.",
                "No listing dates are inferred from first/last stored observations.",
                "Percentages require sourced listing evidence; they do not certify price accuracy.",
                "Off-calendar prices and observations outside listing windows need review.",
                "Every repair candidate needs manual review; SQLite remains unchanged.",
            ],
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only, source-aware coverage audit")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--through", type=date.fromisoformat, default=None)
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--listing-evidence", type=Path)
    parser.add_argument("--json", type=Path, help="Save full report outside SQLite")
    parser.add_argument("--manifest-json", type=Path, help="Save review-only repair candidates")
    args = parser.parse_args(argv)
    from finintel.storage.db import database_path

    db = args.db or database_path()
    outputs = [out.resolve() for out in (args.json, args.manifest_json) if out is not None]
    if len(outputs) != len(set(outputs)) or db.resolve() in outputs:
        parser.error("Output paths must be distinct and must not overwrite SQLite")
    through = args.through or (datetime.now(UTC).date() - timedelta(days=1))
    try:
        listing = load_listing_evidence(args.listing_evidence, through=through)
        report = audit_coverage(db, through=through, years=args.years, listing_evidence=listing)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.manifest_json:
            args.manifest_json.parent.mkdir(parents=True, exist_ok=True)
            args.manifest_json.write_text(
                json.dumps({"read_only": True, "candidates": report["repair_manifest"]}, indent=2)
                + "\n",
                encoding="utf-8",
            )
        print(
            json.dumps(
                {
                    "series_count": report["series_count"],
                    "summary": report["summary"],
                    "repair_candidates_count": report["repair_candidates_count"],
                },
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Coverage audit failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
