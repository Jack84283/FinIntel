"""Reads remain available while serialized storage operations are in progress."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from finintel.market_data import index_history as index_history_module
from finintel.market_data.index_history import store_index_closes
from finintel.storage import backup as backup_module
from finintel.storage.backup import create_backup, restore_backup
from finintel.storage.db import _write_connection, search_symbols


def initialized_database(path):
    conn = _write_connection(path)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO symbol_catalog "
            "(symbol,name,asset_type,exchange,source,updated_at) "
            "VALUES ('AAPL','Apple Inc.','Stock','XNYS','test','2026-10-28')"
        )
    conn.close()


def assert_search_during_blocked_write(db, operation, monkeypatch):
    entered = Event()
    release = Event()
    original = operation(entered, release, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        write = executor.submit(original)
        assert entered.wait(timeout=2)
        result = search_symbols(query="AAPL", db=db)
        assert result["results"][0]["symbol"] == "AAPL"
        release.set()
        write.result(timeout=5)


def test_symbol_search_is_not_blocked_by_backup(tmp_path, monkeypatch):
    db = tmp_path / "finintel.sqlite3"
    initialized_database(db)

    def operation(entered, release, patch):
        original = backup_module._snapshot

        def blocked(path, reason):
            entered.set()
            assert release.wait(timeout=5)
            return original(path, reason)

        patch.setattr(backup_module, "_snapshot", blocked)
        return lambda: create_backup(db=db)

    assert_search_during_blocked_write(db, operation, monkeypatch)


def test_symbol_search_is_not_blocked_by_restore(tmp_path, monkeypatch):
    db = tmp_path / "finintel.sqlite3"
    initialized_database(db)
    backup = create_backup(db=db)

    def operation(entered, release, patch):
        original = backup_module._copy_sqlite

        def blocked(source, destination):
            entered.set()
            assert release.wait(timeout=5)
            return original(source, destination)

        patch.setattr(backup_module, "_copy_sqlite", blocked)
        return lambda: restore_backup(backup["id"], "replace", db=db)

    assert_search_during_blocked_write(db, operation, monkeypatch)


def test_symbol_search_is_not_blocked_by_index_acquisition(tmp_path, monkeypatch):
    db = tmp_path / "finintel.sqlite3"
    initialized_database(db)

    def operation(entered, release, patch):
        original = index_history_module._write_connection

        def blocked(path):
            entered.set()
            assert release.wait(timeout=5)
            return original(path)

        patch.setattr(index_history_module, "_write_connection", blocked)
        return lambda: store_index_closes("SP500", [("2026-10-28", "5800")], db=db)

    assert_search_during_blocked_write(db, operation, monkeypatch)
