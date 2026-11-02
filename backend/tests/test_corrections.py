"""Issue #29: explicit sourced actions and reversible, audit-backed corrections."""

import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.corrections import (
    CorrectionConflict,
    get_correction,
    list_corporate_actions,
    list_corrections,
    preview_correction,
    propose_correction,
    record_verified_actions,
    resolved_close,
    transition_correction,
)
from finintel.market_data.quality import _candidate, _persist, review_finding
from finintel.storage import db as db_module
from finintel.storage.backup import create_backup, restore_backup
from finintel.storage.db import StorageError, _write_connection


def seed(path):
    conn = _write_connection(path)
    with conn:
        conn.execute("INSERT INTO instruments VALUES ('AAPL','2026-10-21')")
        conn.execute(
            "INSERT INTO series(id,symbol,provider,frequency,adjustment,data_kind) "
            "VALUES (1,'AAPL','alpaca_sip','1d','raw_unadjusted','historical_eod')"
        )
        conn.execute(
            "INSERT INTO ingestion_runs (id,series_id,retrieved_at,imported_at,"
            "payload_sha256,source_url,provider_terms_url,first_session,last_session,"
            "observations,inserted,updated,unchanged,stale) "
            "VALUES (1,1,'2026-10-22T00:00:00+00:00','2026-10-22T00:00:00+00:00',"
            "'digest','https://example.test/source','https://example.test/terms',"
            "'2026-10-21','2026-10-21',1,1,0,0,0)"
        )
        conn.execute(
            "INSERT INTO daily_prices VALUES "
            "(1,'2026-10-21','98','102','97','100',1000,'2026-10-22T00:00:00+00:00',1)"
        )
        conn.execute(
            "INSERT INTO index_daily_closes VALUES "
            "('SP500','fred','2026-10-21','5000','2026-10-22T00:00:00+00:00',"
            "'https://fred.stlouisfed.org/series/SP500')"
        )
    conn.close()
    equity = _candidate(
        series_id=1, code="AAPL", provider="alpaca_sip",
        session_date="2026-10-21", finding_type="price_anomaly",
        severity="warning", confidence="low", rule="price_jump_candidate",
        evidence={"possible_corporate_action": True},
        adjustment="raw_unadjusted", frequency="1d",
    )
    index = _candidate(
        instrument_kind="index", series_id=None, code="SP500", provider="fred",
        session_date="2026-10-21", finding_type="price_anomaly",
        severity="warning", confidence="low", rule="price_jump_candidate",
        evidence={"candidate_only": True}, adjustment="price_index", frequency="1d",
    )
    _persist(path, [equity, index])
    with sqlite3.connect(path) as conn:
        equity_id = conn.execute(
            "SELECT id FROM data_quality_findings WHERE code='AAPL'"
        ).fetchone()[0]
        index_id = conn.execute(
            "SELECT id FROM data_quality_findings WHERE code='SP500'"
        ).fetchone()[0]
    return equity_id, index_id


def original_rows(path):
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT * FROM daily_prices").fetchall(),
            conn.execute("SELECT * FROM index_daily_closes").fetchall(),
        )


def test_correction_workflow_is_explicit_and_reversible(tmp_path):
    path = tmp_path / "local.sqlite3"
    equity_id, _ = seed(path)
    prior = original_rows(path)
    with pytest.raises(CorrectionConflict):
        propose_correction(
            path, finding_id=equity_id, provider="alpaca_sip",
            proposed_close="99.5", rationale="Needs confirmation",
        )
    review_finding(path, equity_id, "confirmed", "Source observation requires correction")
    proposal = propose_correction(
        path, finding_id=equity_id, provider="alpaca_sip",
        proposed_close="99.5", rationale="Verified a data entry error",
    )
    key = proposal["id"]
    assert proposal["state"] == "proposed"
    assert [e["to_state"] for e in proposal["events"]] == ["proposed"]
    assert not preview_correction(path, key)["stale_source"]
    assert not preview_correction(path, key)["modifies_stored_prices"]
    assert preview_correction(path, key)["eligible_for_validation"]
    original = resolved_close(
        path, kind="equity", code="AAPL", provider="alpaca_sip",
        adjustment="raw_unadjusted", frequency="1d", session_date="2026-10-21",
    )
    assert original["effective_close"] == "100"
    assert not original["overlay_applied"]

    validated = transition_correction(path, key, "validate")
    assert validated["state"] == "validated"
    approved = transition_correction(path, key, "approve", "Reviewed source and evidence")
    assert approved["state"] == "approved"
    assert [e["to_state"] for e in approved["events"]] == [
        "proposed", "validated", "approved",
    ]
    effective = resolved_close(
        path, kind="equity", code="AAPL", provider="alpaca_sip",
        adjustment="raw_unadjusted", frequency="1d", session_date="2026-10-21",
    )
    assert effective["effective_close"] == "99.5"
    assert effective["overlay_applied"]
    assert effective["source"]["close"] == "100"
    assert original_rows(path) == prior
    assert len(transition_correction(path, key, "approve", "Repeat")["events"]) == 3

    reversed_proposal = transition_correction(
        path, key, "reverse", "Independent review invalidated correction",
    )
    assert reversed_proposal["state"] == "reversed"
    assert len(reversed_proposal["events"]) == 4
    assert resolved_close(
        path, kind="equity", code="AAPL", provider="alpaca_sip",
        adjustment="raw_unadjusted", frequency="1d", session_date="2026-10-21",
    )["effective_close"] == "100"
    assert original_rows(path) == prior
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM quality_correction_events WHERE correction_id=?", (key,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE quality_corrections SET proposed_close='88' WHERE id=?", (key,))


def test_reject_conflicts_and_invalid_semantics(tmp_path):
    path = tmp_path / "local.sqlite3"
    equity_id, _ = seed(path)
    review_finding(path, equity_id, "confirmed", "Reviewed")
    with pytest.raises(ValueError):
        propose_correction(
            path, finding_id=equity_id, provider="tiingo",
            proposed_close="101", rationale="Wrong provider",
        )
    with pytest.raises(ValueError):
        propose_correction(
            path, finding_id=equity_id, provider="alpaca_sip",
            proposed_close="NaN", rationale="Invalid",
        )
    first = propose_correction(
        path, finding_id=equity_id, provider="alpaca_sip",
        proposed_close="95", rationale="One finding",
    )["id"]
    second = propose_correction(
        path, finding_id=equity_id, provider="alpaca_sip",
        proposed_close="96", rationale="Alternative finding",
    )["id"]
    transition_correction(path, first, "validate")
    transition_correction(path, first, "approve", "Checked first")
    with pytest.raises(CorrectionConflict):
        transition_correction(path, second, "validate")
    with pytest.raises(CorrectionConflict):
        transition_correction(path, first, "reject", "Already approved")
    transition_correction(path, first, "reverse", "Return to raw")
    assert transition_correction(path, second, "validate")["state"] == "validated"
    assert transition_correction(path, second, "reject", "No longer justified")["state"] == "rejected"
    with pytest.raises(CorrectionConflict):
        transition_correction(path, second, "approve", "No")


def test_source_revision_invalidates_approved_overlay(tmp_path):
    path = tmp_path / "local.sqlite3"
    equity_id, _ = seed(path)
    review_finding(path, equity_id, "confirmed", "Reviewed")
    key = propose_correction(
        path, finding_id=equity_id, provider="alpaca_sip",
        proposed_close="90", rationale="Test source revision",
    )["id"]
    transition_correction(path, key, "validate")
    transition_correction(path, key, "approve", "Approved for this revision")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE daily_prices SET close='105',retrieved_at='2026-10-23T00:00:00+00:00' "
            "WHERE series_id=1 AND session_date='2026-10-21'"
        )
    resolved = resolved_close(
        path, kind="equity", code="AAPL", provider="alpaca_sip",
        adjustment="raw_unadjusted", frequency="1d", session_date="2026-10-21",
    )
    assert resolved["effective_close"] == "105"
    assert resolved["stale_overlay"]
    assert not resolved["overlay_applied"]
    assert preview_correction(path, key)["stale_source"]
    assert transition_correction(path, key, "reverse", "Provider revised price")["state"] == "reversed"


def test_index_semantics_price_and_volatility_not_equivalent(tmp_path):
    path = tmp_path / "local.sqlite3"
    _, index_id = seed(path)
    review_finding(path, index_id, "confirmed", "Review price index")
    key = propose_correction(
        path, finding_id=index_id, provider="fred",
        proposed_close="5001", rationale="Index level correction",
    )["id"]
    p = get_correction(path, key)
    assert p["adjustment"] == "price_index"
    assert p["return_semantics"] == "price_index"
    transition_correction(path, key, "validate")
    transition_correction(path, key, "approve", "Reviewed price index")
    assert resolved_close(
        path, kind="index", code="SP500", provider="fred",
        adjustment="price_index", frequency="daily", session_date="2026-10-21",
    )["effective_close"] == "5001"
    with pytest.raises(ValueError):
        resolved_close(
            path, kind="index", code="SP500", provider="fred",
            adjustment="total_return_index", frequency="daily", session_date="2026-10-21",
        )


def test_sourced_corporate_event_remains_separate_from_suspected(tmp_path, monkeypatch):
    path = tmp_path / "local.sqlite3"
    seed(path)
    monkeypatch.setattr(
        "finintel.market_data.corrections.verify_events",
        lambda _path, _symbol, client=None: {
            "symbol": "AAPL", "event_count": 2,
            "events": [
                {"date": "2026-10-21", "split_factor": "2",
                 "cash_dividend": "0", "check": "manual_review"},
                {"date": "2026-10-22", "split_factor": "1",
                 "cash_dividend": "0.2", "check": "dividend_reference_only"},
            ],
        },
    )
    verified = record_verified_actions(path, "AAPL")
    assert verified["evidence_records"] == 2
    history = list_corporate_actions(path, "AAPL")
    assert len(history["sourced"]) == 2
    assert all(e["status"] == "sourced" for e in history["sourced"])
    assert history["suspected"][0]["status"] == "suspected"
    assert list_corrections(path)["total"] == 0


def test_correction_api_requires_managed_token(tmp_path, monkeypatch):
    path = tmp_path / "local.sqlite3"
    equity_id, _ = seed(path)
    review_finding(path, equity_id, "confirmed", "Reviewed")
    token = "s" * 32
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", token)
    with TestClient(app) as unauth:
        response = unauth.post(
            "/api/v1/quality/corrections",
            json={"finding_id": equity_id, "provider": "alpaca_sip",
                  "proposed_close": "91", "rationale": "Test"},
        )
        assert response.status_code == 403
    with TestClient(app, headers={"x-finintel-process-token": token}) as client:
        created = client.post(
            "/api/v1/quality/corrections",
            json={"finding_id": equity_id, "provider": "alpaca_sip",
                  "proposed_close": "91", "rationale": "Reviewed override"},
        )
        assert created.status_code == 200, created.text
        key = created.json()["id"]
        assert client.get(f"/api/v1/quality/corrections/{key}/preview").status_code == 200
        assert client.post(
            f"/api/v1/quality/corrections/{key}/validate", json={},
        ).status_code == 200
        approved = client.post(
            f"/api/v1/quality/corrections/{key}/approve", json={"note": "Approved"},
        )
        assert approved.status_code == 200, approved.text
        resolved = client.get(
            "/api/v1/quality/corrections/resolve",
            params={"instrument_kind": "equity", "code": "AAPL", "provider": "alpaca_sip",
                    "adjustment": "raw_unadjusted", "frequency": "1d",
                    "session_date": "2026-10-21"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["effective_close"] == "91"
        assert client.get(
            "/api/v1/quality/corrections", params={"code": "AAPL"},
        ).json()["total"] == 1
        assert client.post(
            f"/api/v1/quality/corrections/{key}/reverse",
            json={"note": "Superseded correction"},
        ).status_code == 200


def test_v14_migration_keeps_prices_and_is_rollback_safe(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    seed(path)
    before = original_rows(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE quality_correction_events")
        conn.execute("DROP TABLE quality_corrections")
        conn.execute("DROP TABLE corporate_action_evidence")
        conn.execute("PRAGMA user_version = 14")
    original = db_module.SCHEMA_V15_CORRECTION_TABLES
    monkeypatch.setattr(db_module, "SCHEMA_V15_CORRECTION_TABLES", (*original, "INVALID SQL"))
    with pytest.raises(StorageError):
        _write_connection(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='quality_corrections'"
        ).fetchone() is None
    monkeypatch.setattr(db_module, "SCHEMA_V15_CORRECTION_TABLES", original)
    migrated = _write_connection(path)
    migrated.close()
    assert original_rows(path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_correction_backup_replace_merge_idempotent_and_conflict_safe(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    equity_id, _ = seed(source)
    review_finding(source, equity_id, "confirmed", "Verified")
    key = propose_correction(
        source, finding_id=equity_id, provider="alpaca_sip",
        proposed_close="92", rationale="Backup merge",
    )["id"]
    transition_correction(source, key, "validate")
    transition_correction(source, key, "approve", "Approved locally")
    before = original_rows(source)
    snap = create_backup(db=source)
    seed(destination)
    result = restore_backup(snap["id"], "merge", db=destination)
    assert result["status"] == "restored"
    merged = get_correction(destination, key)
    assert merged["state"] == "approved"
    assert [row["to_state"] for row in merged["events"]] == [
        "proposed", "validated", "approved",
    ]
    assert original_rows(source) == before
    restored = restore_backup(snap["id"], "merge", db=destination)
    assert restored["status"] == "restored"
    assert len(get_correction(destination, key)["events"]) == 3
    replaced = restore_backup(snap["id"], "replace", db=destination)
    assert replaced["status"] == "restored"
    assert len(get_correction(destination, key)["events"]) == 3
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
