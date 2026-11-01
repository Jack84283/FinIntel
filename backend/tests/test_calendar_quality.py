"""Regression tests for exchange-calendar missing-session classification."""

import sqlite3
from datetime import date

from finintel.market_data.calendar_quality import (
    CalendarEvidence,
    SuspensionEvidence,
    classify_missing_session_gap,
    classify_missing_sessions,
    default_calendar_name,
)
from finintel.market_data.quality import list_findings, run_quality_scan
from finintel.storage.db import _write_connection


def test_holidays_are_not_missing_sessions():
    holiday_boundaries = (
        (date(2025, 11, 26), date(2025, 11, 28)),  # Thanksgiving
        (date(2025, 12, 24), date(2025, 12, 26)),  # Christmas
        (date(2025, 12, 31), date(2026, 1, 2)),  # New Year's Day
        (date(2025, 4, 17), date(2025, 4, 21)),  # Good Friday
    )

    for calendar_name in ("XNYS", "XNAS"):
        for previous, following in holiday_boundaries:
            assert (
                classify_missing_session_gap(
                    previous,
                    following,
                    calendar_name=calendar_name,
                    provider="alpaca_sip",
                    as_of=following,
                )
                == []
            )


def test_exceptional_full_closures_are_not_missing_sessions():
    for previous, following in (
        (date(2018, 12, 4), date(2018, 12, 6)),  # President George H. W. Bush
        (date(2025, 1, 8), date(2025, 1, 10)),  # President Jimmy Carter
    ):
        assert (
            classify_missing_session_gap(
                previous,
                following,
                calendar_name="XNYS",
                provider="alpaca_sip",
                as_of=following,
            )
            == []
        )


def test_dst_boundary_uses_exchange_session_dates_and_timezone():
    records = classify_missing_session_gap(
        date(2026, 3, 6),
        date(2026, 3, 10),
        calendar_name="XNYS",
        provider="alpaca_sip",
        as_of=date(2026, 3, 10),
    )

    assert [record["session_date"] for record in records] == [date(2026, 3, 9)]
    assert records[0]["evidence"]["calendar_timezone"] == "America/New_York"
    assert records[0]["evidence"]["calendar_source"] == "exchange_calendars"


def test_listing_and_delisting_boundaries_exclude_ineligible_sessions():
    records = classify_missing_sessions(
        {date(2026, 1, 1), date(2026, 1, 10)},
        calendar_name="XNYS",
        provider="tiingo",
        expected_start=date(2026, 1, 1),
        expected_end=date(2026, 1, 10),
        evidence={
            "listed_on": "2026-01-05",
            "delisted_on": "2026-01-07",
            "source": "documented-security-master",
            "verified_at": "2026-01-10",
        },
        as_of=date(2026, 1, 10),
    )

    assert [record["session_date"] for record in records] == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    ]
    assert all(record["evidence"]["listing_boundary"]["source"] for record in records)


def test_documented_suspension_is_expected_closure():
    records = classify_missing_session_gap(
        date(2026, 3, 6),
        date(2026, 3, 10),
        calendar_name="XNYS",
        provider="alpaca_sip",
        evidence=CalendarEvidence(
            calendar_name="XNYS",
            source="exchange-halt-bulletin",
            suspensions=(
                SuspensionEvidence(
                    date(2026, 3, 9), date(2026, 3, 9), "exchange-halt-bulletin", "trading halt"
                ),
            ),
        ),
        as_of=date(2026, 3, 10),
    )

    assert len(records) == 1
    assert records[0]["classification"] == "expected_closure"
    assert records[0]["severity"] == "info"
    assert records[0]["evidence"]["suspension"]["source"] == "exchange-halt-bulletin"


def test_recent_provider_delay_is_pending_not_missing():
    records = classify_missing_sessions(
        {date(2026, 10, 30)},
        calendar_name="XNYS",
        provider="tiingo",
        expected_start=date(2026, 10, 30),
        expected_end=date(2026, 11, 2),
        as_of=date(2026, 11, 2),
    )

    assert len(records) == 1
    assert records[0]["session_date"] == date(2026, 11, 2)
    assert records[0]["classification"] == "pending_publication"
    assert records[0]["evidence"]["publication_delay_sessions"] == 1


def test_unavailable_calendar_remains_unresolved(monkeypatch):
    monkeypatch.setattr("finintel.market_data.calendar_quality._calendar", lambda _name: None)
    records = classify_missing_session_gap(
        date(2025, 11, 26),
        date(2025, 11, 28),
        calendar_name="XNYS",
        provider="alpaca_sip",
        as_of=date(2025, 11, 28),
    )

    assert len(records) == 1
    assert records[0]["session_date"] == date(2025, 11, 27)
    assert records[0]["classification"] == "unresolved"
    assert records[0]["severity"] == "info"
    assert records[0]["evidence"]["calendar_available"] is False


def test_index_calendar_mapping_keeps_fred_on_market_sessions():
    assert default_calendar_name(instrument_kind="index", code="SP500") == "XNYS"
    assert default_calendar_name(instrument_kind="index", code="NASDAQCOM") == "XNAS"
    assert (
        classify_missing_session_gap(
            date(2025, 4, 17),
            date(2025, 4, 21),
            calendar_name=default_calendar_name(instrument_kind="index", code="SP500"),
            provider="fred",
            as_of=date(2025, 4, 21),
        )
        == []
    )


def _seed_quality_calendar_database(path):
    conn = _write_connection(path)
    conn.execute("INSERT INTO instruments VALUES ('AAPL', '2026-01-01')")
    conn.execute(
        "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
        "VALUES (1, 'AAPL', 'alpaca_sip', '1d', 'raw_unadjusted', 'historical_eod')"
    )
    ingestion_id = conn.execute(
        "INSERT INTO ingestion_runs "
        "(series_id,retrieved_at,imported_at,payload_sha256,source_url,provider_terms_url,"
        "first_session,last_session,observations,inserted,updated,unchanged,stale) "
        "VALUES (1,'2026-10-31','2026-10-31','hash','source','terms',"
        "'2026-10-19','2026-10-21',2,2,0,0,0) RETURNING id"
    ).fetchone()[0]
    conn.executemany(
        "INSERT INTO daily_prices "
        "(series_id,session_date,open,high,low,close,volume,retrieved_at,ingestion_id) "
        "VALUES (1,?,'100','101','99','100',10,'2026-10-31',?)",
        [("2026-10-19", ingestion_id), ("2026-10-21", ingestion_id)],
    )
    conn.execute(
        "UPDATE symbol_catalog SET exchange='NASDAQ',source='test',updated_at='2026-10-31' "
        "WHERE symbol='AAPL'"
    )
    conn.commit()
    conn.close()


def test_scan_persists_calendar_evidence_with_missing_session_finding(tmp_path):
    db = tmp_path / "quality.sqlite3"
    _seed_quality_calendar_database(db)

    result = run_quality_scan(
        db,
        calendar_evidence={
            1: {
                "suspensions": [
                    {
                        "start": "2026-10-20",
                        "end": "2026-10-20",
                        "source": "exchange-halt-bulletin",
                        "reason": "documented trading halt",
                    }
                ]
            }
        },
        as_of=date(2026, 10, 21),
    )

    assert result["calendar_rule_version"] == "calendar-missing-session-v1"
    findings = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert findings["total"] == 1
    item = findings["items"][0]
    assert item["session_date"] == "2026-10-20"
    assert item["evidence"]["classification"] == "expected_closure"
    assert item["evidence"]["calendar_name"] == "XNAS"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 2
