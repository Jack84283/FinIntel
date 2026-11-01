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
from finintel.market_data.quality import (
    _candidate,
    _persist,
    list_findings,
    review_finding,
    run_quality_scan,
)
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


def test_calendar_validation_and_anomaly_detection_share_one_scan(tmp_path):
    db = tmp_path / "calendar-and-anomaly.sqlite3"
    _seed_quality_calendar_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE daily_prices SET open='249',high='251',low='248',close='250',volume=20000 "
            "WHERE series_id=1 AND session_date='2026-10-21'"
        )
        conn.execute(
            "UPDATE daily_prices SET volume=200 WHERE series_id=1 AND session_date='2026-10-19'"
        )
        conn.commit()

    result = run_quality_scan(db, as_of=date(2026, 10, 21))

    assert result["calendar_rule_version"] == "calendar-missing-session-v1"
    assert result["anomaly_rule_version"] == "anomaly-detection-v1"
    missing = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    anomalies = list_findings(db, finding_type="price_anomaly", code="AAPL", limit=10)
    volume = list_findings(db, finding_type="ohlcv_anomaly", code="AAPL", limit=10)
    assert missing["total"] == 1
    assert missing["items"][0]["evidence"]["classification"] == "suspected_missing"
    assert any(item["rule"] == "price_jump_candidate" for item in anomalies["items"])
    assert any(item["rule"] == "volume_spike_candidate" for item in volume["items"])


def test_missing_session_classification_updates_one_reviewed_finding(tmp_path):
    db = tmp_path / "quality.sqlite3"
    _seed_quality_calendar_database(db)

    first = run_quality_scan(db, as_of=date(2026, 10, 21))
    assert first["complete"] is True
    initial = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert initial["total"] == 1
    assert initial["items"][0]["evidence"]["classification"] == "suspected_missing"
    finding_id = initial["items"][0]["id"]
    review_finding(db, finding_id, "confirmed", "Needs sourced session evidence")

    second = run_quality_scan(
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

    assert second["complete"] is True
    updated = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert updated["total"] == 1
    item = updated["items"][0]
    assert item["id"] == finding_id
    assert item["rule"] == "calendar_missing_session"
    assert item["severity"] == "info"
    assert item["confidence"] == "high"
    assert item["evidence"]["classification"] == "expected_closure"
    assert item["review_state"] == "confirmed"


def test_calendar_scan_supersedes_legacy_weekday_gap_without_losing_review_history(tmp_path):
    db = tmp_path / "populated-pr38.sqlite3"
    _seed_quality_calendar_database(db)

    legacy = _candidate(
        series_id=1,
        code="AAPL",
        provider="alpaca_sip",
        session_date="2026-10-21",
        finding_type="missing_session",
        severity="warning",
        confidence="low",
        rule="weekday_gap_candidate",
        adjustment="raw_unadjusted",
        frequency="1d",
        evidence={
            "previous_session": "2026-10-19",
            "next_session": "2026-10-21",
            "weekday_candidates": ["2026-10-20"],
            "calendar_applied": False,
        },
    )
    _persist(db, [legacy])
    old = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert old["total"] == 1
    old_id = old["items"][0]["id"]
    review_finding(db, old_id, "confirmed", "Reviewed before calendar validation")

    result = run_quality_scan(db, as_of=date(2026, 10, 21))
    assert result["complete"] is True
    active = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert active["total"] == 1
    assert active["items"][0]["rule"] == "calendar_missing_session"
    assert active["items"][0]["session_date"] == "2026-10-20"

    with sqlite3.connect(db) as conn:
        legacy_row = conn.execute(
            "SELECT rule,review_state,superseded_at,supersession_reason "
            "FROM data_quality_findings WHERE id = ?",
            (old_id,),
        ).fetchone()
        assert legacy_row[0] == "weekday_gap_candidate"
        assert legacy_row[1] == "confirmed"
        assert legacy_row[2] is not None
        assert legacy_row[3] == "Reclassified by calendar-aware missing-session validation"
        assert conn.execute(
            "SELECT finding_id FROM data_quality_reviews WHERE finding_id = ?", (old_id,)
        ).fetchone() == (old_id,)


def test_symbol_catalog_exchange_changes_invalidate_scan_checkpoint(tmp_path):
    db = tmp_path / "quality.sqlite3"
    _seed_quality_calendar_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE symbol_catalog SET exchange='' WHERE symbol='AAPL'")
        conn.commit()

    first = run_quality_scan(db, max_findings=1, as_of=date(2026, 10, 21))
    assert first["status"] == "partial"

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE symbol_catalog SET exchange='NASDAQ',updated_at='2026-11-01' "
            "WHERE symbol='AAPL'"
        )
        conn.commit()

    second = run_quality_scan(db, max_findings=1, as_of=date(2026, 10, 21))
    assert second["resumed"] is False
    assert second["cursor_start"] == 0
    findings = list_findings(db, finding_type="missing_session", code="AAPL", limit=10)
    assert findings["total"] == 1
    assert findings["items"][0]["evidence"]["calendar_available"] is True
    assert findings["items"][0]["evidence"]["calendar_name"] == "XNAS"
