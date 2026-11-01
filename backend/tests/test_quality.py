"""Durable data-quality candidate and review behavior."""

import json
import sqlite3
import threading
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data import quality as quality_module
from finintel.market_data.quality import (
    _candidate,
    _fingerprint,
    _legacy_fingerprint,
    _persist,
    list_findings,
    quality_report,
    review_finding,
    run_quality_scan,
)
from finintel.storage import db as db_module
from finintel.storage.db import STORAGE_WRITE_LOCK, _write_connection


def seed_quality_database(path):
    conn = _write_connection(path)
    conn.execute("INSERT INTO instruments VALUES ('AAPL', '2026-01-01')")
    conn.execute("INSERT INTO instruments VALUES ('TSLA', '2026-01-01')")
    series = [
        (1, "AAPL", "alpaca_sip"),
        (2, "AAPL", "tiingo"),
        (3, "TSLA", "alpaca_sip"),
    ]
    for series_id, symbol, provider in series:
        conn.execute(
            "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
            "VALUES (?, ?, ?, '1d', 'raw_unadjusted', 'historical_eod')",
            (series_id, symbol, provider),
        )
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id, series_id, retrieved_at, imported_at, payload_sha256, source_url, "
            "provider_terms_url, first_session, last_session, observations, inserted, updated, unchanged, stale) "
            "VALUES (?, ?, '2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00', ?, ?, ?, '2026-10-19', '2026-10-21', 2, 2, 0, 0, 0)",
            (
                series_id,
                series_id,
                f"hash-{series_id}",
                f"https://example.test/{provider}",
                "https://example.test/terms",
            ),
        )
    rows = [
        (1, "2026-10-19", "99", "101", "98", "100", 10, 1),
        (1, "2026-10-21", "249", "251", "248", "250", 10, 1),
        (2, "2026-10-21", "199", "201", "198", "200", 10, 2),
        # The database layer preserves imported rows; the quality scanner flags
        # this locally invalid candle without rewriting it.
        (3, "2026-10-20", "10", "9", "8", "10", 10, 3),
    ]
    conn.executemany(
        "INSERT INTO daily_prices "
        "(series_id, session_date, open, high, low, close, volume, retrieved_at, ingestion_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-31T00:00:00+00:00', ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO index_daily_closes "
        "(index_code, provider, session_date, close, retrieved_at, source_url) "
        "VALUES ('SP500', 'fred', ?, ?, '2026-10-31T00:00:00+00:00', 'https://fred.stlouisfed.org/series/SP500')",
        [("2026-10-19", "5000"), ("2026-10-21", "10000")],
    )
    conn.commit()
    conn.close()


def test_quality_scan_persists_redacted_candidates_without_changing_prices(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    before = (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id, session_date, open, high, low, close, volume FROM daily_prices ORDER BY series_id"
        )
        .fetchall()
    )

    result = run_quality_scan(db)
    types = {item["finding_type"] for item in result["examples"]}
    assert {"missing_session", "price_anomaly", "ohlcv_anomaly", "provider_divergence"} <= types
    assert result["finding_count"] >= 6
    assert any(item["instrument_kind"] == "index" for item in result["examples"])
    assert all("example.test" not in item["evidence"] for item in result["examples"])
    assert (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id, session_date, open, high, low, close, volume FROM daily_prices ORDER BY series_id"
        )
        .fetchall()
        == before
    )

    stored = list_findings(db, limit=100)
    assert stored["total"] == result["finding_count"]
    assert all(item["review_state"] == "candidate" for item in stored["items"])


def test_quality_fingerprint_includes_full_series_identity(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
            "VALUES (?, 'AAPL', 'alpaca_sip', ?, ?, 'historical_eod')",
            [(4, "1wk", "raw_unadjusted"), (5, "1d", "adjusted")],
        )

    common = {
        "code": "AAPL",
        "provider": "alpaca_sip",
        "session_date": "2026-10-21",
        "finding_type": "ohlcv_anomaly",
        "severity": "error",
        "confidence": "high",
        "rule": "validated_ohlcv_constraints",
        "evidence": {"invalid_fields": ["ohlc_order"]},
    }
    candidates = [
        _candidate(series_id=1, adjustment="raw_unadjusted", frequency="1d", **common),
        _candidate(series_id=4, adjustment="raw_unadjusted", frequency="1wk", **common),
        _candidate(series_id=5, adjustment="adjusted", frequency="1d", **common),
    ]
    assert len({item["fingerprint"] for item in candidates}) == 3

    _persist(db, candidates)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == 3
    _persist(db, candidates)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_findings").fetchone()[0] == 3


def test_quality_scan_detects_repeated_ingestion_payloads(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
            "provider_terms_url,first_session,last_session,observations,inserted,"
            "updated,unchanged,stale) VALUES "
            "(4,1,'2026-10-31T01:00:00+00:00','2026-10-31T01:00:00+00:00',"
            "'hash-1','https://example.test/alpaca','https://example.test/terms',"
            "'2026-10-19','2026-10-21',2,0,0,2,0)"
        )

    run_quality_scan(db, as_of=date(2026, 11, 1))
    findings = list_findings(db, finding_type="duplicate", code="AAPL", limit=10)

    assert findings["total"] == 1
    evidence = findings["items"][0]["evidence"]
    assert evidence["duplicate_source_observations"] is True
    assert evidence["occurrences"] == 2
    assert evidence["anomaly_rule_version"] == "anomaly-detection-v1"


def test_quality_scan_keeps_distinct_repeated_payload_groups(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO ingestion_runs "
            "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
            "provider_terms_url,first_session,last_session,observations,inserted,"
            "updated,unchanged,stale) VALUES "
            "(?,1,?,?,?,'https://example.test/alpaca','https://example.test/terms',"
            "'2026-10-19','2026-10-21',2,0,0,2,0)",
            [
                (4, "2026-10-31T01:00:00+00:00", "2026-10-31T01:00:00+00:00", "hash-1"),
                (5, "2026-10-31T02:00:00+00:00", "2026-10-31T02:00:00+00:00", "hash-2"),
                (6, "2026-10-31T03:00:00+00:00", "2026-10-31T03:00:00+00:00", "hash-2"),
            ],
        )

    run_quality_scan(db, as_of=date(2026, 11, 1))
    findings = list_findings(db, finding_type="duplicate", code="AAPL", limit=10)

    assert findings["total"] == 2
    assert {item["evidence"]["payload_sha256"] for item in findings["items"]} == {
        "hash-1",
        "hash-2",
    }


def test_quality_scan_preserves_legacy_price_jump_review_history(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    legacy = _candidate(
        series_id=1,
        code="AAPL",
        provider="alpaca_sip",
        adjustment="raw_unadjusted",
        frequency="1d",
        session_date="2026-10-21",
        finding_type="price_anomaly",
        severity="warning",
        confidence="low",
        rule="close_jump_over_2x_or_under_half",
        evidence={"previous_close": "100", "close": "250"},
    )
    _persist(db, [legacy])
    before = list_findings(db, finding_type="price_anomaly", code="AAPL", limit=10)
    finding_id = before["items"][0]["id"]
    review_finding(db, finding_id, "confirmed", "Reviewed as a candidate split.")

    run_quality_scan(db)

    after = list_findings(db, finding_type="price_anomaly", code="AAPL", limit=10)
    upgraded = [item for item in after["items"] if item["rule"] == "price_jump_candidate"]
    assert len(upgraded) == 1
    assert upgraded[0]["id"] == finding_id
    assert upgraded[0]["review_state"] == "confirmed"


def test_completed_scan_reactivates_reviewed_finding_after_threshold_restoration(tmp_path):
    db = tmp_path / "threshold-reconciliation.sqlite3"
    seed_quality_database(db)
    first = run_quality_scan(
        db,
        thresholds={"price_jump_ratio": "2"},
        as_of=date(2026, 11, 1),
    )
    finding = next(
        item
        for item in list_findings(db, finding_type="price_anomaly", code="AAPL", limit=20)["items"]
        if item["rule"] == "price_jump_candidate"
    )
    finding_id = finding["id"]
    original_evidence = finding["evidence"]
    review_finding(db, finding_id, "confirmed", "Reviewed before threshold change")

    second = run_quality_scan(
        db,
        thresholds={"price_jump_ratio": "3"},
        as_of=date(2026, 11, 1),
    )

    assert first["complete"] is True
    assert second["complete"] is True
    assert second["findings_superseded"] >= 1
    assert not any(
        item["id"] == finding_id
        for item in list_findings(db, finding_type="price_anomaly", code="AAPL", limit=20)["items"]
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT review_state,evidence_json,superseded_at,supersession_reason "
            "FROM data_quality_findings WHERE id=?",
            (finding_id,),
        ).fetchone()
        assert row[0] == "confirmed"
        assert json.loads(row[1]) == original_evidence
        assert row[2] is not None
        assert row[3] == "No longer detected by completed full quality scan"
        assert conn.execute(
            "SELECT finding_id,from_state,to_state,note FROM data_quality_reviews "
            "WHERE finding_id=?",
            (finding_id,),
        ).fetchone()[:3] == (finding_id, "candidate", "confirmed")
    assert quality_report(db, limit=100)["complete"] is True

    third = run_quality_scan(
        db,
        thresholds={"price_jump_ratio": "2"},
        as_of=date(2026, 11, 1),
    )

    assert third["complete"] is True
    reactivated = next(
        item
        for item in list_findings(db, finding_type="price_anomaly", code="AAPL", limit=20)["items"]
        if item["rule"] == "price_jump_candidate"
    )
    assert reactivated["id"] == finding_id
    assert reactivated["review_state"] == "confirmed"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT review_state,superseded_at,supersession_reason FROM data_quality_findings "
            "WHERE id=?",
            (finding_id,),
        ).fetchone()
        assert row == ("confirmed", None, None)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id=?", (finding_id,)
            ).fetchone()[0]
            == 1
        )


def test_completed_scan_supersedes_refreshed_stale_observation(tmp_path):
    db = tmp_path / "stale-reconciliation.sqlite3"
    seed_quality_database(db)
    first = run_quality_scan(db, as_of=date(2027, 1, 15))
    stale = next(
        item
        for item in list_findings(db, finding_type="stale_observation", code="AAPL", limit=20)[
            "items"
        ]
        if item["rule"] == "stale_latest_observation"
    )
    stale_id = stale["id"]
    review_finding(db, stale_id, "confirmed", "Reviewed before refreshed observation")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE ingestion_runs SET retrieved_at=?, imported_at=?, "
            "provider_last_refreshed=? WHERE series_id IN (1,2)",
            (
                "2027-01-14T00:00:00+00:00",
                "2027-01-14T00:00:00+00:00",
                "2027-01-14T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE daily_prices SET retrieved_at=? WHERE series_id IN (1,2)",
            ("2027-01-14T00:00:00+00:00",),
        )
        conn.commit()

    second = run_quality_scan(db, as_of=date(2027, 1, 15))

    assert first["complete"] is True
    assert second["complete"] is True
    assert second["findings_superseded"] >= 1
    assert list_findings(db, finding_type="stale_observation", code="AAPL", limit=20)["total"] == 0
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT review_state,superseded_at,supersession_reason "
            "FROM data_quality_findings WHERE id=?",
            (stale_id,),
        ).fetchone()
        assert row[0] == "confirmed"
        assert row[1] is not None
        assert row[2] == "No longer detected by completed full quality scan"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id=?",
                (stale_id,),
            ).fetchone()[0]
            == 1
        )

    third = run_quality_scan(db, as_of=date(2027, 2, 20))

    assert third["complete"] is True
    reactivated = [
        item
        for item in list_findings(db, finding_type="stale_observation", code="AAPL", limit=20)[
            "items"
        ]
        if item["id"] == stale_id
    ]
    assert len(reactivated) == 1
    assert reactivated[0]["review_state"] == "confirmed"
    assert reactivated[0]["evidence"]["validation_reference_date"] == "2027-02-20"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT review_state,superseded_at,supersession_reason FROM data_quality_findings "
            "WHERE id=?",
            (stale_id,),
        ).fetchone()
        assert row == ("confirmed", None, None)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id=?", (stale_id,)
            ).fetchone()[0]
            == 1
        )


def test_partial_scan_does_not_supersede_until_full_resume_completes(tmp_path):
    db = tmp_path / "partial-reconciliation.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db, thresholds={"price_jump_ratio": "2"}, as_of=date(2026, 11, 1))
    finding = next(
        item
        for item in list_findings(db, finding_type="price_anomaly", code="AAPL", limit=20)["items"]
        if item["rule"] == "price_jump_candidate"
    )
    finding_id = finding["id"]
    review_finding(db, finding_id, "rejected", "Reviewed before partial rescan")

    result = run_quality_scan(
        db,
        max_findings=1,
        thresholds={"price_jump_ratio": "3"},
        as_of=date(2026, 11, 1),
    )
    assert result["complete"] is False
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT superseded_at,review_state FROM data_quality_findings WHERE id=?",
            (finding_id,),
        ).fetchone() == (None, "rejected")

    for _ in range(20):
        if result["complete"]:
            break
        result = run_quality_scan(
            db,
            max_findings=1,
            thresholds={"price_jump_ratio": "3"},
            as_of=date(2026, 11, 1),
        )
    assert result["complete"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT superseded_at,review_state FROM data_quality_findings WHERE id=?",
            (finding_id,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "rejected"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id=?", (finding_id,)
            ).fetchone()[0]
            == 1
        )


def test_resumed_partial_scan_reactivates_finding_when_candidate_is_reached(tmp_path):
    db = tmp_path / "partial-reactivation.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db, thresholds={"price_jump_ratio": "2"}, as_of=date(2026, 11, 1))
    finding = next(
        item
        for item in list_findings(db, finding_type="price_anomaly", code="SP500", limit=20)["items"]
        if item["rule"] == "price_jump_candidate"
    )
    finding_id = finding["id"]
    review_finding(db, finding_id, "confirmed", "Reviewed before resumed reactivation")

    run_quality_scan(db, thresholds={"price_jump_ratio": "3"}, as_of=date(2026, 11, 1))
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT superseded_at,review_state FROM data_quality_findings WHERE id=?",
                (finding_id,),
            ).fetchone()[0]
            is not None
        )

    scan_kwargs = {
        "max_findings": 1,
        "thresholds": {"price_jump_ratio": "2"},
        "as_of": date(2026, 11, 1),
    }
    result = run_quality_scan(db, **scan_kwargs)
    assert result["complete"] is False
    assert result["resumed"] is False
    assert result["findings_superseded"] == 0
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT superseded_at FROM data_quality_findings WHERE id=?",
                (finding_id,),
            ).fetchone()[0]
            is not None
        )

    reactivated_during_partial = False
    for _ in range(20):
        result = run_quality_scan(db, **scan_kwargs)
        assert result["findings_superseded"] == 0
        if result["complete"]:
            break
        with sqlite3.connect(db) as conn:
            superseded_at = conn.execute(
                "SELECT superseded_at FROM data_quality_findings WHERE id=?",
                (finding_id,),
            ).fetchone()[0]
        if superseded_at is None:
            assert result["resumed"] is True
            reactivated_during_partial = True
            break

    assert reactivated_during_partial is True
    for _ in range(20):
        if result["complete"]:
            break
        result = run_quality_scan(db, **scan_kwargs)
    assert result["complete"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT review_state,superseded_at,supersession_reason "
            "FROM data_quality_findings WHERE id=?",
            (finding_id,),
        ).fetchone() == ("confirmed", None, None)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id=?", (finding_id,)
            ).fetchone()[0]
            == 1
        )


def test_pr39_dimensioned_index_price_jump_upgrade_reuses_reviewed_finding(tmp_path):
    db = tmp_path / "pr39-index-price-jump.sqlite3"
    seed_quality_database(db)
    legacy = _candidate(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        adjustment="price_index",
        frequency="1d",
        session_date="2026-10-21",
        finding_type="price_anomaly",
        severity="warning",
        confidence="low",
        rule="close_jump_over_2x_or_under_half",
        evidence={"previous_close": "5000", "close": "10000"},
    )
    _persist(db, [legacy])
    finding_id = list_findings(db, finding_type="price_anomaly", code="SP500", limit=10)["items"][
        0
    ]["id"]
    review_finding(db, finding_id, "confirmed", "Reviewed before anomaly engine upgrade")

    run_quality_scan(db, as_of=date(2026, 10, 21))

    findings = list_findings(db, finding_type="price_anomaly", code="SP500", limit=10)
    upgraded = [item for item in findings["items"] if item["rule"] == "price_jump_candidate"]
    assert len(upgraded) == 1
    assert upgraded[0]["id"] == finding_id
    assert upgraded[0]["review_state"] == "confirmed"
    assert upgraded[0]["fingerprint"] == _fingerprint(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        session_date="2026-10-21",
        finding_type="price_anomaly",
        rule="price_jump_candidate",
        adjustment="price_index",
        frequency="1d",
    )


def test_pr39_dimensioned_index_price_jump_collision_preserves_both_review_states(tmp_path):
    db = tmp_path / "pr39-index-price-jump-collision.sqlite3"
    seed_quality_database(db)
    legacy = _candidate(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        adjustment="price_index",
        frequency="1d",
        session_date="2026-10-21",
        finding_type="price_anomaly",
        severity="warning",
        confidence="low",
        rule="close_jump_over_2x_or_under_half",
        evidence={"previous_close": "5000", "close": "10000"},
    )
    current = _candidate(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        adjustment="price_index",
        frequency="1d",
        session_date="2026-10-21",
        finding_type="price_anomaly",
        severity="warning",
        confidence="low",
        rule="price_jump_candidate",
        evidence={"previous_close": "5000", "close": "10000"},
    )
    conn = _write_connection(db)
    for finding_id, candidate in ((41, legacy), (42, current)):
        conn.execute(
            "INSERT INTO data_quality_findings("
            "id,instrument_kind,code,series_id,provider,session_date,finding_type,"
            "severity,confidence,rule,evidence_json,fingerprint,review_state,"
            "resolution_note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                finding_id,
                candidate["instrument_kind"],
                candidate["code"],
                candidate["series_id"],
                candidate["provider"],
                candidate["session_date"],
                candidate["finding_type"],
                candidate["severity"],
                candidate["confidence"],
                candidate["rule"],
                "{}",
                candidate["fingerprint"],
                "candidate",
                None,
                "2026-10-31T00:00:00+00:00",
                "2026-10-31T00:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()
    review_finding(db, 41, "confirmed", "Legacy review")
    review_finding(db, 42, "rejected", "Restored duplicate review")

    run_quality_scan(db, as_of=date(2026, 10, 21))

    with sqlite3.connect(db) as check:
        rows = check.execute(
            "SELECT id,fingerprint,rule,review_state,superseded_at "
            "FROM data_quality_findings WHERE id IN (41,42) ORDER BY id"
        ).fetchall()
        assert rows[0][0:4] == (
            41,
            current["fingerprint"],
            "price_jump_candidate",
            "confirmed",
        )
        assert rows[0][4] is None
        assert rows[1][0] == 42
        assert rows[1][2] == "price_jump_candidate"
        assert rows[1][3] == "rejected"
        assert rows[1][4] is not None
        history = check.execute(
            "SELECT finding_id,from_state,to_state,note FROM data_quality_reviews ORDER BY id"
        ).fetchall()
        assert history[0][0:3] == (41, "candidate", "confirmed")
        assert history[1][0:3] == (42, "candidate", "rejected")
        assert history[2][0:3] == (41, "confirmed", "confirmed")
        assert "duplicate finding #42" in history[2][3]


def test_quality_report_marks_ingestion_metadata_revision_stale(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db, as_of=date(2026, 11, 1))
    assert quality_report(db)["complete"] is True

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
            "provider_terms_url,first_session,last_session,observations,inserted,"
            "updated,unchanged,stale) VALUES "
            "(4,1,'2026-10-31T01:00:00+00:00','2026-10-31T01:00:00+00:00',"
            "'hash-1','https://example.test/alpaca','https://example.test/terms',"
            "'2026-10-19','2026-10-21',2,0,0,2,0)"
        )
        conn.commit()

    report = quality_report(db)
    assert report["stale"] is True
    assert report["complete"] is False
    assert report["scan_completeness"]["source_revision_changed"] is True


def test_quality_scan_detects_outdated_provider_revision(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
            "provider_terms_url,provider_last_refreshed,first_session,last_session,"
            "observations,inserted,updated,unchanged,stale) VALUES "
            "(4,1,'2026-10-31T01:00:00+00:00','2026-10-31T01:00:00+00:00',"
            "'hash-revision','https://example.test/alpaca','https://example.test/terms',"
            "'2026-10-20T00:00:00+00:00','2026-10-19','2026-10-21',2,0,0,0,1)"
        )

    run_quality_scan(db, as_of=date(2026, 11, 1))
    findings = list_findings(db, finding_type="stale_observation", code="AAPL", limit=10)

    assert findings["total"] == 1
    evidence = findings["items"][0]["evidence"]
    assert evidence["revision_status"] == "provider_revision_requires_review"
    assert evidence["stale_imports"][0]["stale_observations"] == 1


def test_quality_scan_detects_stale_latest_observation_with_configured_age(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    run_quality_scan(db, as_of=date(2026, 12, 10), thresholds={"stale_after_days": 30})
    findings = list_findings(db, finding_type="stale_observation", code="AAPL", limit=10)

    assert any(item["rule"] == "stale_latest_observation" for item in findings["items"])


def test_validation_reference_date_refreshes_stale_findings_and_report_freshness(tmp_path):
    db = tmp_path / "quality-reference-date.sqlite3"
    seed_quality_database(db)

    run_quality_scan(db, as_of=date(2026, 11, 1))
    assert quality_report(db, as_of=date(2026, 11, 1))["complete"] is True
    changed_date_report = quality_report(db, as_of=date(2026, 11, 2))
    assert changed_date_report["stale"] is True
    assert changed_date_report["complete"] is False
    assert changed_date_report["validation_reference_date_changed"] is True

    run_quality_scan(db, as_of=date(2026, 12, 10))
    stale = list_findings(db, finding_type="stale_observation", code="AAPL", limit=10)
    stale_latest = [
        item
        for item in stale["items"]
        if item["rule"] == "stale_latest_observation" and item["series_id"] == 1
    ]
    assert len(stale_latest) == 1
    finding_id = stale_latest[0]["id"]
    assert stale_latest[0]["evidence"]["validation_reference_date"] == "2026-12-10"
    review_finding(db, finding_id, "confirmed", "Reviewed at the first validation date")

    run_quality_scan(db, as_of=date(2027, 1, 15))
    refreshed = list_findings(db, finding_type="stale_observation", code="AAPL", limit=10)
    refreshed_latest = [
        item
        for item in refreshed["items"]
        if item["rule"] == "stale_latest_observation" and item["series_id"] == 1
    ]
    assert refreshed_latest[0]["id"] == finding_id
    assert refreshed_latest[0]["review_state"] == "confirmed"
    assert refreshed_latest[0]["evidence"]["validation_reference_date"] == "2027-01-15"
    assert quality_report(db, as_of=date(2027, 1, 15))["complete"] is True


def test_quality_scan_flags_liquid_volume_spike_but_not_illiquid_noise(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO daily_prices "
            "(series_id,session_date,open,high,low,close,volume,retrieved_at,ingestion_id) "
            "VALUES (3,?,'10','11','9','10',?,'2026-10-31T00:00:00+00:00',3)",
            [("2026-10-17", 1_000), ("2026-10-18", 1_100), ("2026-10-19", 20_000)],
        )
        conn.commit()

    run_quality_scan(db, as_of=date(2026, 11, 1))
    findings = list_findings(db, finding_type="ohlcv_anomaly", code="TSLA", limit=50)

    assert any(item["rule"] == "volume_spike_candidate" for item in findings["items"])
    assert all(item["evidence"].get("baseline_volume", "0") != "1" for item in findings["items"])


def test_quality_scan_detects_repeated_index_payloads(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO index_ingestion_runs "
            "(id,index_code,provider,requested_start,requested_end,retrieved_at,source_url,"
            "observation_sha256,payload_sha256,first_session,last_session,observations,"
            "inserted,updated,unchanged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "SP500",
                    "fred",
                    "2026-10-19",
                    "2026-10-21",
                    "2026-10-31T00:00:00+00:00",
                    "https://example.test/fred",
                    "observation-hash",
                    "payload-hash",
                    "2026-10-19",
                    "2026-10-21",
                    2,
                    2,
                    0,
                    0,
                ),
                (
                    2,
                    "SP500",
                    "fred",
                    "2026-10-19",
                    "2026-10-21",
                    "2026-10-31T01:00:00+00:00",
                    "https://example.test/fred",
                    "observation-hash-revised",
                    "payload-hash",
                    "2026-10-19",
                    "2026-10-21",
                    2,
                    0,
                    0,
                    2,
                ),
            ],
        )
        conn.commit()

    run_quality_scan(db, as_of=date(2026, 11, 1))
    findings = list_findings(db, finding_type="duplicate", code="SP500", limit=10)

    assert findings["total"] == 1
    assert findings["items"][0]["instrument_kind"] == "index"
    revisions = list_findings(db, finding_type="stale_observation", code="SP500", limit=10)
    assert revisions["total"] == 1
    assert revisions["items"][0]["rule"] == "outdated_provider_revision"


def test_repeated_quality_scans_reuse_finding_rows(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    first = run_quality_scan(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT id, fingerprint FROM data_quality_findings ORDER BY id"
        ).fetchall()

    second = run_quality_scan(db)
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT id, fingerprint FROM data_quality_findings ORDER BY id"
        ).fetchall()

    assert second["complete"] is True
    assert second["truncated"] is False
    assert second["finding_count"] == first["finding_count"]
    assert after == before


def test_pr38_index_anomaly_upgrade_reuses_reviewed_finding(tmp_path):
    db = tmp_path / "pr38.sqlite3"
    conn = _write_connection(db)
    conn.execute(
        "INSERT INTO index_daily_closes"
        "(index_code,provider,session_date,close,retrieved_at,source_url) "
        "VALUES ('SP500','fred',?,?,?,?)",
        (
            "2026-10-20",
            "0",
            "2026-10-31T00:00:00+00:00",
            "https://fred.stlouisfed.org/series/SP500",
        ),
    )

    # Recreate the PR #38 quality tables around a real populated database. In
    # particular, the old table has no lifecycle columns and the stored hash
    # is the PR #38 JSON identity with null index dimensions.
    conn.execute("DROP TABLE data_quality_reviews")
    conn.execute("DROP TABLE data_quality_findings")
    conn.execute(
        """
        CREATE TABLE data_quality_findings (
            id INTEGER PRIMARY KEY,
            instrument_kind TEXT NOT NULL CHECK(instrument_kind IN ('equity', 'index')),
            code TEXT NOT NULL,
            series_id INTEGER REFERENCES series(id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            session_date TEXT,
            finding_type TEXT NOT NULL CHECK(finding_type IN (
                'missing_session', 'price_anomaly', 'ohlcv_anomaly',
                'provider_divergence', 'duplicate', 'stale_observation'
            )),
            severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error')),
            confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
            rule TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE,
            review_state TEXT NOT NULL DEFAULT 'candidate' CHECK(
                review_state IN ('candidate', 'confirmed', 'rejected', 'accepted_correction')
            ),
            resolution_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE data_quality_reviews (
            id INTEGER PRIMARY KEY,
            finding_id INTEGER NOT NULL REFERENCES data_quality_findings(id) ON DELETE CASCADE,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            note TEXT,
            reviewed_at TEXT NOT NULL
        )
        """
    )
    legacy_fingerprint = _legacy_fingerprint(
        code="SP500",
        provider="fred",
        session_date="2026-10-20",
        finding_type="price_anomaly",
        rule="positive_finite_index_close",
    )
    conn.execute(
        "INSERT INTO data_quality_findings("
        "id,instrument_kind,code,series_id,provider,session_date,finding_type,"
        "severity,confidence,rule,evidence_json,fingerprint,review_state,"
        "resolution_note,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            41,
            "index",
            "SP500",
            None,
            "fred",
            "2026-10-20",
            "price_anomaly",
            "error",
            "high",
            "positive_finite_index_close",
            '{"close":"0"}',
            legacy_fingerprint,
            "confirmed",
            "Confirmed during PR #38 review",
            "2026-10-31T01:00:00+00:00",
            "2026-10-31T02:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO data_quality_reviews"
        "(id,finding_id,from_state,to_state,note,reviewed_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            73,
            41,
            "candidate",
            "confirmed",
            "Confirmed during PR #38 review",
            "2026-10-31T02:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 14
        assert "superseded_at" not in {
            row[1] for row in check.execute("PRAGMA table_info(data_quality_findings)")
        }

    result = run_quality_scan(db, as_of=date(2026, 10, 20))
    assert result["complete"] is True
    expected_fingerprint = _fingerprint(
        instrument_kind="index",
        series_id=None,
        code="SP500",
        provider="fred",
        session_date="2026-10-20",
        finding_type="price_anomaly",
        rule="positive_finite_index_close",
        adjustment="price_index",
        frequency="1d",
    )
    with sqlite3.connect(db) as check:
        rows = check.execute(
            "SELECT id,fingerprint,review_state,resolution_note,superseded_at "
            "FROM data_quality_findings WHERE instrument_kind='index'"
        ).fetchall()
        assert rows == [
            (41, expected_fingerprint, "confirmed", "Confirmed during PR #38 review", None)
        ]
        assert check.execute(
            "SELECT id,finding_id,from_state,to_state,note FROM data_quality_reviews"
        ).fetchall() == [(73, 41, "candidate", "confirmed", "Confirmed during PR #38 review")]


def test_pr38_index_anomaly_collision_preserves_legacy_review_state(tmp_path):
    db = tmp_path / "pr38-collision.sqlite3"
    conn = _write_connection(db)
    conn.execute(
        "INSERT INTO index_daily_closes"
        "(index_code,provider,session_date,close,retrieved_at,source_url) "
        "VALUES ('SP500','fred',?,?,?,?)",
        (
            "2026-10-20",
            "0",
            "2026-10-31T00:00:00+00:00",
            "https://fred.stlouisfed.org/series/SP500",
        ),
    )
    finding = {
        "instrument_kind": "index",
        "code": "SP500",
        "series_id": None,
        "provider": "fred",
        "session_date": "2026-10-20",
        "finding_type": "price_anomaly",
        "severity": "error",
        "confidence": "high",
        "rule": "positive_finite_index_close",
        "evidence_json": '{"close":"0"}',
    }
    legacy_fingerprint = _legacy_fingerprint(
        code=finding["code"],
        provider=finding["provider"],
        session_date=finding["session_date"],
        finding_type=finding["finding_type"],
        rule=finding["rule"],
    )
    current_fingerprint = _fingerprint(
        instrument_kind="index",
        series_id=None,
        code=finding["code"],
        provider=finding["provider"],
        session_date=finding["session_date"],
        finding_type=finding["finding_type"],
        rule=finding["rule"],
        adjustment="price_index",
        frequency="1d",
    )
    conn.executemany(
        "INSERT INTO data_quality_findings("
        "id,instrument_kind,code,series_id,provider,session_date,finding_type,"
        "severity,confidence,rule,evidence_json,fingerprint,review_state,"
        "resolution_note,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                41,
                *finding.values(),
                legacy_fingerprint,
                "confirmed",
                "Confirmed during PR #38 review",
                "2026-10-31T01:00:00+00:00",
                "2026-10-31T02:00:00+00:00",
            ),
            (
                42,
                *finding.values(),
                current_fingerprint,
                "rejected",
                "Duplicate reviewed after restore",
                "2026-10-31T03:00:00+00:00",
                "2026-10-31T04:00:00+00:00",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO data_quality_reviews"
        "(id,finding_id,from_state,to_state,note,reviewed_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            (
                73,
                41,
                "candidate",
                "confirmed",
                "Confirmed during PR #38 review",
                "2026-10-31T02:00:00+00:00",
            ),
            (
                74,
                42,
                "candidate",
                "rejected",
                "Duplicate reviewed after restore",
                "2026-10-31T04:00:00+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    result = run_quality_scan(db, as_of=date(2026, 10, 20))
    assert result["complete"] is True
    with sqlite3.connect(db) as check:
        rows = check.execute(
            "SELECT id,fingerprint,review_state,resolution_note,superseded_at "
            "FROM data_quality_findings WHERE instrument_kind='index' ORDER BY id"
        ).fetchall()
        assert rows[0] == (
            41,
            current_fingerprint,
            "confirmed",
            "Confirmed during PR #38 review",
            None,
        )
        assert rows[1][0] == 42
        assert rows[1][1] != current_fingerprint
        assert rows[1][2] == "rejected"
        assert rows[1][3] == "Duplicate reviewed after restore"
        assert rows[1][4] is not None
        assert (
            check.execute(
                "SELECT COUNT(*) FROM data_quality_findings WHERE superseded_at IS NULL"
            ).fetchone()[0]
            == 1
        )
        history = check.execute(
            "SELECT id,finding_id,from_state,to_state,note FROM data_quality_reviews ORDER BY id"
        ).fetchall()
        assert history[0] == (
            73,
            41,
            "candidate",
            "confirmed",
            "Confirmed during PR #38 review",
        )
        assert history[1] == (
            74,
            42,
            "candidate",
            "rejected",
            "Duplicate reviewed after restore",
        )
        assert history[2][1:4] == (41, "confirmed", "confirmed")
        assert "duplicate finding #42" in history[2][4]


def test_truncated_quality_scan_resumes_from_durable_cursor(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    first = run_quality_scan(db, max_findings=2)
    assert first["truncated"] is True
    assert first["cursor_start"] == 0
    assert first["cursor_next"] == 2
    assert first["findings_examined"] == first["findings_stored"] == 2

    second = run_quality_scan(db, max_findings=2)
    assert second["resumed"] is True
    assert second["cursor_start"] == first["cursor_next"]
    assert second["persisted_findings_total"] > first["persisted_findings_total"]

    result = second
    for _ in range(10):
        if result["complete"]:
            break
        result = run_quality_scan(db, max_findings=2)
    assert result["complete"] is True
    assert result["truncated"] is False
    assert result["persisted_findings_total"] == result["total_known_findings"]
    with sqlite3.connect(db) as conn:
        progress = conn.execute(
            "SELECT status,cursor_next,stale FROM data_quality_scan_progress WHERE scan_key='all'"
        ).fetchone()
        assert progress == ("completed", 0, 0)


def test_bounded_batches_preserve_all_candidates_without_scanning_remaining_history(
    tmp_path, monkeypatch
):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    observed_rows = []
    original = quality_module._iter_query_rows

    def tracked_rows(*args, **kwargs):
        for row in original(*args, **kwargs):
            observed_rows.append(row)
            yield row

    monkeypatch.setattr(quality_module, "_iter_query_rows", tracked_rows)
    result = run_quality_scan(db, max_findings=2)

    assert result["status"] == "partial"
    assert result["findings_stored"] == 2
    assert len(observed_rows) == 2
    assert result["cursor_source_key"] == {
        "series_id": 1,
        "session_date": "2026-10-21",
    }

    bounded = [item["fingerprint"] for item in result["examples"]]
    while not result["complete"]:
        result = run_quality_scan(db, max_findings=2)
        bounded.extend(item["fingerprint"] for item in result["examples"])
    assert result["persisted_findings_total"] == result["total_known_findings"]

    reference = tmp_path / "reference.sqlite3"
    seed_quality_database(reference)
    full = run_quality_scan(reference)
    assert set(bounded) <= {item["fingerprint"] for item in full["examples"]}


def test_resumed_scan_restores_checkpoint_volume_window(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM index_daily_closes")
        conn.execute("DELETE FROM daily_prices")
        conn.execute("DELETE FROM ingestion_runs")
        conn.execute("DELETE FROM series")
        conn.execute("DELETE FROM instruments WHERE symbol='AAPL'")
        conn.execute(
            "INSERT INTO series(id,symbol,provider,frequency,adjustment,data_kind) "
            "VALUES (1,'TSLA','alpaca_sip','1d','raw_unadjusted','historical_eod')"
        )
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(id,series_id,retrieved_at,imported_at,payload_sha256,source_url,"
            "provider_terms_url,first_session,last_session,observations,inserted,"
            "updated,unchanged,stale) VALUES "
            "(1,1,'2026-10-31T00:00:00+00:00','2026-10-31T00:00:00+00:00',"
            "'hash-tsla','https://example.test/alpaca','https://example.test/terms',"
            "'2026-10-20','2026-10-21',2,2,0,0,0)"
        )
        conn.executemany(
            "INSERT INTO daily_prices "
            "(series_id,session_date,open,high,low,close,volume,retrieved_at,ingestion_id) "
            "VALUES (1,?,'10','9','8','10',?,'2026-10-31T00:00:00+00:00',1)",
            [("2026-10-20", 1_000), ("2026-10-21", 20_000)],
        )

    first = run_quality_scan(db, max_findings=1, as_of=date(2026, 11, 1))
    assert first["cursor_source_key"] == {
        "series_id": 1,
        "session_date": "2026-10-20",
    }

    second = run_quality_scan(db, max_findings=1, as_of=date(2026, 11, 1))

    assert any(item["rule"] == "volume_spike_candidate" for item in second["examples"])


def test_resumed_quality_scan_seeks_from_phase_source_key(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    first = run_quality_scan(db, max_findings=2)
    assert first["cursor_phase"] == "equity"
    assert first["cursor_source_key"] == {
        "series_id": 1,
        "session_date": "2026-10-21",
    }

    observed_start_keys = []
    original = quality_module._iter_query_rows

    def tracked_rows(*args, **kwargs):
        observed_start_keys.append(kwargs.get("start_key"))
        yield from original(*args, **kwargs)

    monkeypatch.setattr(quality_module, "_iter_query_rows", tracked_rows)
    second = run_quality_scan(db, max_findings=2)

    assert second["resumed"] is True
    assert (1, "2026-10-21") in observed_start_keys
    with sqlite3.connect(db) as conn:
        progress = conn.execute(
            "SELECT cursor_phase,cursor_source_key FROM data_quality_scan_progress"
        ).fetchone()
        assert progress[0] in {"equity", "index", "cross_provider"}
        assert progress[1] is not None


def test_quality_scan_reports_truncation_and_report_counts_all_known_findings(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)

    result = run_quality_scan(db, max_findings=2)
    assert result["status"] == "partial"
    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["findings_examined"] == result["findings_stored"] == 2
    assert result["total_known_findings"] == result["findings_examined"]
    assert result["persisted_findings_total"] == 2

    report = quality_report(db, limit=2)
    assert report["status"] == "partial"
    assert report["complete"] is False
    assert report["truncated"] is False
    assert report["total_known_findings"] == result["total_known_findings"]
    assert report["findings_stored"] == 2
    assert report["returned_count"] == 2
    assert report["pagination"]["truncated"] is False
    assert report["scan_completeness"]["truncated"] is True
    assert sum(report["review_states"].values()) == 2


def test_quality_report_marks_scan_stale_after_daily_import(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    scan = run_quality_scan(db)
    assert scan["complete"] is True
    before = quality_report(db, limit=100)
    assert before["complete"] is True

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(series_id,session_date,open,high,low,close,volume,retrieved_at,ingestion_id) "
            "VALUES (1,'2026-10-22','101','102','100','101',10,'2026-11-01T00:00:00+00:00',1)"
        )
        conn.commit()

    after = quality_report(db, limit=100)
    assert after["status"] == "stale"
    assert after["stale"] is True
    assert after["complete"] is False
    assert after["scan_completeness"]["stale"] is True
    assert after["scan_completeness"]["source_revision_changed"] is True
    assert (
        after["scan_completeness"]["source_data_version_current"]
        != after["scan_completeness"]["source_data_version_end"]
    )


def test_quality_report_marks_completed_scan_stale_after_exchange_catalog_change(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    scan = run_quality_scan(db)
    assert scan["complete"] is True
    assert quality_report(db, limit=100)["complete"] is True

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE symbol_catalog SET exchange='XNAS',updated_at='2026-11-01T00:00:00+00:00' "
            "WHERE symbol='AAPL'"
        )
        conn.commit()

    report = quality_report(db, limit=100)
    assert report["status"] == "stale"
    assert report["stale"] is True
    assert report["complete"] is False
    assert report["source_revision_changed"] is False
    assert report["source_catalog_identity_changed"] is True
    assert report["scan_completeness"]["source_catalog_identity_changed"] is True
    assert report["scan_completeness"]["complete"] is False


def test_cross_provider_query_plan_uses_scan_indexes(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        series_plan = [
            row[-1]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id,symbol,provider,frequency,adjustment "
                "FROM series ORDER BY symbol,frequency,adjustment,provider,id"
            )
        ]
        price_plan = [
            row[-1]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT session_date,close FROM daily_prices "
                "WHERE series_id=1 ORDER BY session_date LIMIT 512"
            )
        ]
    assert any("data_quality_series_scan_order" in detail for detail in series_plan)
    assert not any("USE TEMP B-TREE" in detail for detail in series_plan + price_plan)


def test_quality_scan_does_not_hold_write_lock_during_read_only_scan(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors = []
    original = quality_module._read_candidates

    def blocked_read(path, max_findings, **kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the read phase")
        return original(path, max_findings, **kwargs)

    monkeypatch.setattr(quality_module, "_read_candidates", blocked_read)

    def scan():
        try:
            results.append(run_quality_scan(db))
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            db_module.StorageError,
        ) as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=scan)
    thread.start()
    assert entered.wait(5)
    acquired = STORAGE_WRITE_LOCK.acquire(timeout=1)
    release.set()
    if acquired:
        STORAGE_WRITE_LOCK.release()
    thread.join(10)

    assert acquired is True
    assert not thread.is_alive()
    assert errors == []
    assert results and results[0]["status"] == "completed"


def test_quality_scan_allows_a_real_sqlite_writer_between_read_chunks(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db)
    retained_finding_id = list_findings(db, limit=1)["items"][0]["id"]
    scan_paused = threading.Event()
    allow_scan = threading.Event()
    writer_done = threading.Event()
    results = []
    errors = []
    writer_errors = []
    original = quality_module._iter_query_rows

    def paused_rows(*args, **kwargs):
        paused = False
        for row in original(*args, **kwargs):
            if not paused:
                paused = True
                scan_paused.set()
                if not allow_scan.wait(5):
                    raise AssertionError("test did not release the scan")
            yield row

    monkeypatch.setattr(quality_module, "_iter_query_rows", paused_rows)

    def scan():
        try:
            results.append(run_quality_scan(db))
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            db_module.StorageError,
        ) as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def writer():
        try:
            with sqlite3.connect(db, timeout=0.5) as conn:
                conn.execute(
                    "UPDATE daily_prices SET close='101',retrieved_at='2026-11-01' "
                    "WHERE series_id=1 AND session_date='2026-10-19'"
                )
                conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            writer_done.set()

    scan_thread = threading.Thread(target=scan)
    scan_thread.start()
    assert scan_paused.wait(5)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert writer_done.wait(3)
    allow_scan.set()
    scan_thread.join(10)
    writer_thread.join(3)

    assert writer_errors == []
    assert errors == []
    assert not scan_thread.is_alive()
    assert results and results[0]["status"] == "stale"
    assert results[0]["stale"] is True
    assert results[0]["findings_superseded"] == 0
    assert results[0]["source_data_version_end"] != results[0]["source_data_version_start"]
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT superseded_at FROM data_quality_findings WHERE id=?",
                (retained_finding_id,),
            ).fetchone()[0]
            is None
        )
    report = quality_report(db, limit=100)
    assert report["status"] == "stale"
    assert report["complete"] is False
    assert report["scan_completeness"]["stale"] is True


def test_quality_review_write_does_not_mark_scan_stale(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db)
    finding_id = list_findings(db, limit=1)["items"][0]["id"]
    scan_paused = threading.Event()
    allow_scan = threading.Event()
    review_done = threading.Event()
    results = []
    errors = []
    original = quality_module._iter_query_rows

    def paused_rows(*args, **kwargs):
        paused = False
        for row in original(*args, **kwargs):
            if not paused:
                paused = True
                scan_paused.set()
                if not allow_scan.wait(5):
                    raise AssertionError("test did not release the scan")
            yield row

    monkeypatch.setattr(quality_module, "_iter_query_rows", paused_rows)

    def scan():
        try:
            results.append(run_quality_scan(db))
        except (AssertionError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    def reviewer():
        try:
            review_finding(db, finding_id, "confirmed", "Reviewed during scan")
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)
        finally:
            review_done.set()

    scan_thread = threading.Thread(target=scan)
    scan_thread.start()
    assert scan_paused.wait(5)
    review_thread = threading.Thread(target=reviewer)
    review_thread.start()
    assert review_done.wait(3)
    allow_scan.set()
    scan_thread.join(10)
    review_thread.join(3)

    assert errors == []
    assert not scan_thread.is_alive()
    assert results and results[0]["status"] == "completed"
    assert results[0]["stale"] is False


def test_cross_provider_scan_handles_realistic_history_without_materializing_rows(tmp_path):
    db = tmp_path / "performance.sqlite3"
    symbols = [f"T{index:04d}" for index in range(12)]
    sessions = [date(2025, 1, 2) + timedelta(days=index) for index in range(252)]
    conn = _write_connection(db)
    conn.executemany(
        "INSERT INTO instruments(symbol, created_at) VALUES (?, '2026-10-31')",
        [(symbol,) for symbol in symbols],
    )
    series_rows = []
    ingestion_rows = []
    price_rows = []
    next_series = 100
    for symbol in symbols:
        for provider, offset in (("alpaca_sip", "0"), ("tiingo", "2")):
            series_id = next_series
            next_series += 1
            series_rows.append(
                (series_id, symbol, provider, "1d", "raw_unadjusted", "historical_eod")
            )
            ingestion_rows.append(
                (
                    series_id,
                    series_id,
                    f"performance-{series_id}",
                    f"https://example.test/{provider}",
                )
            )
            price_rows.extend(
                (
                    series_id,
                    session.isoformat(),
                    str(98 + index),
                    str(103 + index),
                    str(97 + index),
                    str(100 + index + int(offset)),
                    1000,
                    series_id,
                )
                for index, session in enumerate(sessions)
            )
    conn.executemany(
        "INSERT INTO series(id, symbol, provider, frequency, adjustment, data_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        series_rows,
    )
    conn.executemany(
        "INSERT INTO ingestion_runs "
        "(id, series_id, retrieved_at, imported_at, payload_sha256, source_url, "
        "provider_terms_url, first_session, last_session, observations, inserted, updated, unchanged, stale) "
        "VALUES (?, ?, '2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00', ?, ?, "
        "'https://example.test/terms', '2025-01-02', '2025-09-10', 252, 252, 0, 0, 0)",
        ingestion_rows,
    )
    conn.executemany(
        "INSERT INTO daily_prices "
        "(series_id, session_date, open, high, low, close, volume, retrieved_at, ingestion_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-31T00:00:00+00:00', ?)",
        price_rows,
    )
    conn.commit()
    conn.close()

    started = time.monotonic()
    result = run_quality_scan(db, max_findings=20)
    elapsed = time.monotonic() - started
    assert result["status"] == "partial"
    assert result["findings_examined"] == result["findings_stored"] == 20
    assert elapsed < 10


def test_review_is_explicit_and_preserves_history(tmp_path):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    result = run_quality_scan(db)
    finding = list_findings(db, limit=1)["items"][0]

    reviewed = review_finding(
        db, finding["id"], "accepted_correction", "Reviewed against source evidence"
    )
    assert reviewed["review_state"] == "accepted_correction"
    assert reviewed["resolution_note"] == "Reviewed against source evidence"
    assert quality_report(db)["review_states"] == {
        "accepted_correction": 1,
        "candidate": result["finding_count"] - 1,
    }
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_quality_reviews").fetchone()[0] == 1


def test_v13_database_migrates_to_quality_tables_without_erasing_observations(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE data_quality_reviews")
        conn.execute("DROP TABLE data_quality_findings")
        conn.execute("PRAGMA user_version = 13")
        before = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    conn = _write_connection(db)
    conn.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == before
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_quality_findings'"
        ).fetchone()


def test_failed_quality_migration_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "legacy.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE data_quality_reviews")
        conn.execute("DROP TABLE data_quality_findings")
        conn.execute("PRAGMA user_version = 13")
        before = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]

    original_schema = db_module.SCHEMA_V14_QUALITY_INDEX
    monkeypatch.setattr(
        db_module,
        "SCHEMA_V14_QUALITY_INDEX",
        "CREATE INDEX partial_quality ON data_quality_findings(code); "
        "PRAGMA user_version = 14; CREATE TABLE invalid_quality(",
    )
    with pytest.raises(db_module.StorageError, match="data-quality"):
        _write_connection(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == before
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='partial_quality'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(db_module, "SCHEMA_V14_QUALITY_INDEX", original_schema)
    conn = _write_connection(db)
    conn.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_quality_api_scans_filters_and_reviews_with_managed_token(tmp_path, monkeypatch):
    db = tmp_path / "quality.sqlite3"
    seed_quality_database(db)
    token = "q" * 32
    monkeypatch.setenv("FININTEL_DB_PATH", str(db))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    with TestClient(app, headers={"x-finintel-process-token": token}) as client:
        scan = client.post(
            "/api/v1/quality/scan",
            json={
                "max_findings": 10,
                "as_of": "2026-11-01",
                "thresholds": {"price_jump_ratio": "1.5"},
            },
        )
        assert scan.status_code == 200
        scan_payload = scan.json()
        assert scan_payload["thresholds"]["price_jump_ratio"] == "1.5"
        assert scan_payload["validation_reference_date"] == "2026-11-01"
        assert scan_payload["findings_examined"] == scan_payload["total_known_findings"]
        assert scan_payload["findings_stored"] == scan_payload["finding_count"]
        assert scan_payload["truncated"] is False
        response = client.get(
            "/api/v1/quality/findings",
            params={"finding_type": "provider_divergence", "limit": 10},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        finding_id = items[0]["id"]
        reviewed = client.post(
            f"/api/v1/quality/findings/{finding_id}/review",
            json={"state": "rejected", "note": "Expected adjustment mismatch"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["review_state"] == "rejected"
        report = client.get("/api/v1/quality/report", params={"limit": 1})
        assert report.status_code == 200
        assert report.json()["truncated"] is True
        assert report.json()["scan_completeness"]["complete"] is True
        assert report.json()["total_known_findings"] == scan_payload["findings_stored"]


def test_reconciliation_keeps_existing_review_and_source_rows(tmp_path):
    """Re-running under changed preferences must preserve quality IDs and prices."""

    db = tmp_path / "provider-reconcile.sqlite3"
    seed_quality_database(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT series_id, session_date, close, retrieved_at, ingestion_id "
            "FROM daily_prices ORDER BY series_id, session_date"
        ).fetchall()

    run_quality_scan(db, as_of=date(2026, 11, 1))
    first = next(
        item
        for item in list_findings(db, finding_type="provider_divergence")["items"]
        if item["rule"] == "overlapping_close_differs_over_1pct"
    )
    review_finding(db, first["id"], "confirmed", "Test review: compare source evidence.")
    run_quality_scan(
        db,
        as_of=date(2026, 11, 1),
        thresholds={
            "reconciliation_provider_priority": [
                "tiingo", "alpaca_sip", "alpha_vantage"
            ]
        },
    )
    second = next(
        item
        for item in list_findings(db, finding_type="provider_divergence")["items"]
        if item["fingerprint"] == first["fingerprint"]
    )
    assert second["id"] == first["id"]
    assert second["review_state"] == "confirmed"
    assert second["evidence"]["preferred_provider_for_future_analysis"] == "tiingo"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT series_id, session_date, close, retrieved_at, ingestion_id "
            "FROM daily_prices ORDER BY series_id, session_date"
        ).fetchall() == before
        assert conn.execute(
            "SELECT COUNT(*) FROM data_quality_reviews WHERE finding_id = ?",
            (first["id"],),
        ).fetchone()[0] == 1


def test_reconciliation_endpoint_is_read_only(tmp_path, monkeypatch):
    db = tmp_path / "provider-reconcile.sqlite3"
    seed_quality_database(db)
    run_quality_scan(db, as_of=date(2026, 11, 1))
    monkeypatch.setattr("finintel.market_data.quality_api.database_path", lambda: db)
    with sqlite3.connect(db) as conn:
        count_before = conn.execute(
            "SELECT COUNT(*) FROM data_quality_findings"
        ).fetchone()[0]
    client = TestClient(app)
    policy = client.get("/api/v1/quality/reconciliation/policy")
    assert policy.status_code == 200
    assert "provider_priority" in policy.json()
    response = client.get("/api/v1/quality/reconciliation/summary?code=AAPL")
    assert response.status_code == 200
    assert response.json()["scope"] == "AAPL"
    assert response.json()["findings"]["active"] >= 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM data_quality_findings"
        ).fetchone()[0] == count_before
