"""Offline tests for batch selection and dated index classifications."""

import sqlite3

from finintel.market_data.batch import pending_symbols
from finintel.market_data.membership import load_membership, memberships
from finintel.storage.db import _write_connection


def test_membership_snapshot_tracks_source_without_claiming_etf_constituents(tmp_path):
    path = tmp_path / "fi.sqlite3"
    assert (
        load_membership("SP500", "2026-10-01", "licensed-snapshot", ["AAPL", "MSFT"], db=path) == 2
    )
    assert memberships("AAPL", db=path)[0]["source"] == "licensed-snapshot"
    assert memberships("SPY", db=path) == []
    assert load_membership("SP500", "2026-10-01", "revised-snapshot", ["MSFT"], db=path) == 1
    assert memberships("AAPL", db=path) == []


def test_batch_selection_only_uses_supported_catalog_and_skips_downloaded(tmp_path, monkeypatch):
    path = tmp_path / "fi.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    conn = _write_connection(path)
    with conn:
        conn.execute("INSERT INTO instruments(symbol, created_at) VALUES ('AAPL', 'now')")
        conn.execute(
            "INSERT INTO series(symbol, provider, frequency, adjustment, data_kind) "
            "VALUES('AAPL', 'alpha_vantage', '1d', 'raw_unadjusted', 'ohlcv')"
        )
    conn.close()
    symbols = pending_symbols(provider="alpha_vantage", maximum=100, asset_type="Stock")
    assert "AAPL" not in symbols
    assert "MSFT" in symbols
    assert "SPY" not in symbols
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 12
