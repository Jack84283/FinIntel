"""Action event persistence tests; no provider credentials or synthetic market events."""

import sqlite3

import pytest

from finintel.storage.db import append_action_event, list_action_events


def test_action_event_log_is_append_only_and_bounded(tmp_path):
    db = tmp_path / "events.sqlite3"
    assert list_action_events(db=db) == []
    first = append_action_event(
        symbol="AAPL",
        provider="alpha_vantage",
        status="failed",
        duration_ms=42,
        error_code="http_429",
        db=db,
    )
    second = append_action_event(
        symbol="MSFT", provider="alpaca_sip", status="success", duration_ms=200, import_id=7, db=db
    )
    records = list_action_events(db=db, limit=1)
    assert len(records) == 1
    assert records[0]["id"] == second
    assert records[0]["status"] == "success"
    assert records[0]["import_id"] == 7
    assert first < second
    assert list_action_events(db=db)[1]["error_code"] == "http_429"
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_action_event_rejects_unbounded_or_sensitive_error_details(tmp_path):
    db = tmp_path / "events.sqlite3"
    with pytest.raises(ValueError):
        append_action_event(
            symbol="AAPL",
            provider="alpha_vantage",
            status="failed",
            duration_ms=1,
            error_code="API key=private",
            db=db,
        )
    with pytest.raises(ValueError):
        append_action_event(
            symbol="AAPL", provider="alpha_vantage", status="success", duration_ms=1, db=db
        )
