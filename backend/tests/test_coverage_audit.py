"""Synthetic regression tests for read-only historical coverage classification."""

import csv
import sqlite3
from datetime import date

import pytest

from finintel.market_data.coverage_audit import audit_coverage, load_listing_evidence


def db_with_rows(tmp_path, series, prices, catalog=None):
    db = tmp_path / "sample.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE series (
            id INTEGER PRIMARY KEY, symbol TEXT, provider TEXT,
            adjustment TEXT, frequency TEXT
        );
        CREATE TABLE daily_prices (
            series_id INTEGER, session_date TEXT, PRIMARY KEY(series_id, session_date)
        );
        CREATE TABLE symbol_catalog (
            symbol TEXT PRIMARY KEY, asset_type TEXT, exchange TEXT
        );
        """
    )
    conn.executemany("INSERT INTO series VALUES(?, ?, ?, ?, ?)", series)
    conn.executemany("INSERT INTO daily_prices VALUES(?, ?)", prices)
    conn.executemany("INSERT INTO symbol_catalog VALUES(?, ?, ?)", catalog or [])
    conn.commit()
    conn.close()
    return db


def listing(symbol, listed, through, delisted=None, verified=None):
    return {
        symbol: {
            "listed_on": date.fromisoformat(listed),
            "delisted_on": date.fromisoformat(delisted) if delisted else None,
            "source": "documented-security-master",
            "verified_on": date.fromisoformat(verified or through),
        }
    }


def test_exchange_holiday_and_weekend_are_not_gaps(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "AAPL", "alpaca_sip", "raw_unadjusted", "1d")],
        [(1, "2025-11-26"), (1, "2025-11-28")],
        [("AAPL", "Stock", "NASDAQ")],
    )
    report = audit_coverage(
        db,
        through=date(2025, 11, 28),
        years=1,
        listing_evidence=listing("AAPL", "2025-11-26", "2025-11-28"),
    )
    result = report["series"][0]
    assert result["calendar"] == "XNAS"
    assert result["eligible_sessions"] == 2
    assert result["eligible_gap_candidates"] == 0
    assert result["status"] == "calendar_coverage_present"
    assert report["repair_manifest"] == []


def test_internal_gap_without_listing_evidence_is_not_certified(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "AAPL", "alpaca_sip", "raw_unadjusted", "1d")],
        [(1, "2026-10-19"), (1, "2026-10-22")],
        [("AAPL", "Stock", "NASDAQ")],
    )
    result = audit_coverage(db, through=date(2026, 10, 23), years=1)
    assert result["series"][0]["status"] == "eligibility_unknown"
    assert result["series"][0]["internal_gap_candidates"] == 2
    assert result["series"][0]["eligible_sessions_present_pct"] is None
    assert result["repair_manifest"] == []


def test_verified_listing_produces_review_only_grouped_candidates(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "NEW", "tiingo", "raw_unadjusted", "1d")],
        [(1, "2026-10-19"), (1, "2026-10-22")],
        [("NEW", "Stock", "NYSE")],
    )
    result = audit_coverage(
        db,
        through=date(2026, 10, 23),
        years=1,
        listing_evidence=listing("NEW", "2026-10-19", "2026-10-23"),
    )
    entry = result["series"][0]
    assert entry["eligible_sessions"] == 5
    assert entry["eligible_sessions_present"] == 2
    assert entry["eligible_sessions_present_pct"] == 40
    assert entry["eligible_gap_candidates"] == 3
    assert result["repair_candidates_count"] == 2
    assert result["repair_manifest"][0]["start"] == "2026-10-20"
    assert result["repair_manifest"][0]["end"] == "2026-10-21"
    assert result["repair_manifest"][1]["start"] == "2026-10-23"
    assert all(not item["safe_to_auto_fetch"] for item in result["repair_manifest"])


def test_pre_ipo_and_post_delisting_dates_not_counted_as_gaps(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "NEW", "tiingo", "raw_unadjusted", "1d")],
        [(1, "2026-10-20")],
        [("NEW", "Stock", "NYSE")],
    )
    result = audit_coverage(
        db,
        through=date(2026, 10, 23),
        years=1,
        listing_evidence=listing("NEW", "2026-10-20", "2026-10-23", delisted="2026-10-20"),
    )
    assert result["series"][0]["eligible_sessions"] == 1
    assert result["series"][0]["eligible_gap_candidates"] == 0
    assert result["repair_manifest"] == []


def test_unmapped_exchange_and_index_not_scored(tmp_path):
    db = db_with_rows(
        tmp_path,
        [
            (1, "XYZ", "tiingo", "raw_unadjusted", "1d"),
            (2, "^GSPC", "tiingo", "raw_unadjusted", "1d"),
        ],
        [(1, "2026-10-23"), (2, "2026-10-23")],
        [("XYZ", "Stock", "OTHER"), ("^GSPC", "Index", "NYSE")],
    )
    result = audit_coverage(db, through=date(2026, 10, 23), years=1)
    assert {x["symbol"]: x["status"] for x in result["series"]} == {
        "^GSPC": "unsupported_instrument",
        "XYZ": "calendar_unresolved",
    }
    assert all(x["eligible_sessions_present_pct"] is None for x in result["series"])


def test_empty_series_with_stale_listing_evidence(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "AAPL", "tiingo", "raw_unadjusted", "1d")],
        [],
        [("AAPL", "Stock", "NASDAQ")],
    )
    result = audit_coverage(
        db,
        through=date(2026, 10, 23),
        years=1,
        listing_evidence=listing("AAPL", "2026-10-21", "2026-10-23", verified="2026-10-22"),
    )
    assert result["series"][0]["observed_sessions"] == 0
    assert result["series"][0]["status"] == "stale_listing_evidence"
    assert result["series"][0]["eligible_gap_candidates"] == 3


def test_evidence_csv_requires_sources_and_rejects_duplicates(tmp_path):
    path = tmp_path / "listing.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "listed_on", "delisted_on", "source", "verified_on"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "AAPL",
                "listed_on": "1980-12-12",
                "delisted_on": "",
                "source": "listing-source",
                "verified_on": "2026-10-23",
            }
        )
    result = load_listing_evidence(path, through=date(2026, 10, 23))
    assert result["AAPL"]["listed_on"] == date(1980, 12, 12)
    with path.open("a") as handle:
        handle.write("AAPL,2026-01-01,,duplicated,2026-10-23\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_listing_evidence(path, through=date(2026, 10, 23))


def test_sourced_calendar_override_when_catalog_exchange_missing(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "ABC", "tiingo", "raw_unadjusted", "1d")],
        [(1, "2026-10-23")],
        [("ABC", "Stock", "")],
    )
    evidence = listing("ABC", "2026-10-23", "2026-10-23")
    evidence["ABC"]["calendar"] = "XNAS"
    result = audit_coverage(db, through=date(2026, 10, 23), years=1, listing_evidence=evidence)
    assert result["series"][0]["status"] == "calendar_coverage_present"


def test_unexpected_weekend_bar_cannot_pass(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "ABC", "tiingo", "raw_unadjusted", "1d")],
        [(1, "2026-10-22"), (1, "2026-10-23"), (1, "2026-10-24")],
        [("ABC", "Stock", "NYSE")],
    )
    result = audit_coverage(
        db,
        through=date(2026, 10, 24),
        years=1,
        listing_evidence=listing("ABC", "2026-10-22", "2026-10-24"),
    )
    assert result["series"][0]["off_calendar_observations"] == 1
    assert result["series"][0]["outside_eligibility_observations"] == 1
    assert result["series"][0]["status"] == "needs_review"


def test_sqlite_bytes_unchanged(tmp_path):
    db = db_with_rows(
        tmp_path,
        [(1, "AAPL", "tiingo", "raw_unadjusted", "1d")],
        [(1, "2026-10-23")],
        [("AAPL", "Stock", "NASDAQ")],
    )
    before = db.read_bytes()
    audit_coverage(db, through=date(2026, 10, 23), years=1)
    assert db.read_bytes() == before
