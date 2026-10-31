"""Offline tests for resumable bounded FRED index backfills."""

import json
import os
from datetime import date

from finintel.market_data import index_backfill_worker
from finintel.market_data.provider import ProviderUnavailable


def test_checkpoint_advances_by_chunk_without_duplicate_requests(tmp_path):
    calls = []

    def acquire(code, **kwargs):
        calls.append((code, kwargs["start"], kwargs["end"], kwargs["allow_empty"]))
        return {"status": "stored", "sessions": 2, "inserted": 2, "updated": 0, "unchanged": 0}

    checkpoint = tmp_path / "sp500.json"
    args = {
        "code": "SP500",
        "state_path": checkpoint,
        "start": date(2020, 1, 1),
        "end": date(2020, 1, 5),
        "chunk_days": 2,
        "once": True,
        "acquire": acquire,
    }

    assert index_backfill_worker.run(**args) == 0
    assert index_backfill_worker.run(**args) == 0
    assert index_backfill_worker.run(**args) == 0
    assert calls == [
        ("SP500", date(2020, 1, 1), date(2020, 1, 2), True),
        ("SP500", date(2020, 1, 3), date(2020, 1, 4), True),
        ("SP500", date(2020, 1, 5), date(2020, 1, 5), True),
    ]
    saved = json.loads(checkpoint.read_text())
    assert saved["last_status"] == "stored"
    assert saved["next_start"] == "2020-01-06"
    assert len(saved["completed_ranges"]) == 3


def test_failed_chunk_is_retried_without_repeating_completed_chunks(tmp_path):
    calls = []

    def acquire(code, **kwargs):
        calls.append((kwargs["start"], kwargs["end"]))
        if len(calls) == 2:
            raise ProviderUnavailable("temporary outage")
        return {"status": "stored", "sessions": 1}

    checkpoint = tmp_path / "djia.json"
    args = {
        "code": "DJIA",
        "state_path": checkpoint,
        "start": date(2020, 1, 1),
        "end": date(2020, 1, 4),
        "chunk_days": 2,
        "once": True,
        "acquire": acquire,
    }

    assert index_backfill_worker.run(**args) == 0
    assert index_backfill_worker.run(**args) == 2
    failed = json.loads(checkpoint.read_text())
    assert failed["next_start"] == "2020-01-03"
    assert failed["last_status"] == "paused_provider"

    assert index_backfill_worker.run(**args) == 0
    assert calls == [
        (date(2020, 1, 1), date(2020, 1, 2)),
        (date(2020, 1, 3), date(2020, 1, 4)),
        (date(2020, 1, 3), date(2020, 1, 4)),
    ]


def test_empty_provider_window_is_checkpointed_and_skipped(tmp_path):
    calls = []

    def acquire(_code, **kwargs):
        calls.append((kwargs["start"], kwargs["end"]))
        return {"status": "empty", "sessions": 0}

    checkpoint = tmp_path / "vix.json"
    assert (
        index_backfill_worker.run(
            code="VIX",
            state_path=checkpoint,
            start=date(2020, 1, 1),
            end=date(2020, 1, 1),
            acquire=acquire,
        )
        == 0
    )
    saved = json.loads(checkpoint.read_text())
    assert saved["last_status"] == "complete"
    assert saved["completed_ranges"][0]["status"] == "empty"
    assert calls == [(date(2020, 1, 1), date(2020, 1, 1))]


def test_checkpoint_rejects_changed_range_or_chunk_size(tmp_path):
    checkpoint = tmp_path / "sp500.json"
    checkpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "code": "SP500",
                "start": "2020-01-01",
                "end": "2020-01-05",
                "chunk_days": 2,
                "next_start": "2020-01-01",
                "completed_ranges": [],
            }
        )
    )

    try:
        index_backfill_worker.run(
            code="SP500",
            state_path=checkpoint,
            start=date(2020, 1, 1),
            end=date(2020, 1, 5),
            chunk_days=3,
            acquire=lambda *_args, **_kwargs: {"status": "stored"},
        )
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("Changed backfill parameters must reject the checkpoint")


def test_checkpoint_rejects_database_replacement_after_completed_chunks(tmp_path):
    db = tmp_path / "market.sqlite3"
    checkpoint = tmp_path / "sp500.json"

    def acquire(_code, **kwargs):
        kwargs["db"].write_bytes(b"first database")
        return {"status": "stored", "sessions": 1}

    args = {
        "code": "SP500",
        "state_path": checkpoint,
        "db": db,
        "start": date(2020, 1, 1),
        "end": date(2020, 1, 1),
        "once": True,
        "acquire": acquire,
    }
    assert index_backfill_worker.run(**args) == 0

    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(b"replacement database")
    replacement.replace(db)

    try:
        index_backfill_worker.run(**args)
    except ValueError as exc:
        assert "another database" in str(exc)
    else:
        raise AssertionError("A replaced database must not reuse a completed checkpoint")


def test_default_checkpoint_path_uses_explicit_database(tmp_path, monkeypatch):
    db = tmp_path / "custom.sqlite3"
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(index_backfill_worker, "run", fake_run)
    assert (
        index_backfill_worker.main(
            [
                "--code",
                "SP500",
                "--db",
                str(db),
                "--start",
                "2020-01-01",
                "--end",
                "2020-01-01",
            ]
        )
        == 0
    )
    assert captured["db"] == db
    assert captured["state_path"] == db.with_suffix(".sp500.index-backfill.json")


def test_checkpoint_rejects_symlinked_temporary_file(tmp_path, monkeypatch):
    protected = tmp_path / "protected.txt"
    protected.write_text("do not truncate")
    temporary = tmp_path / ".checkpoint.json.injected.tmp"
    temporary.symlink_to(protected)
    fd = os.open(protected, os.O_RDWR)
    monkeypatch.setattr(
        index_backfill_worker.tempfile,
        "mkstemp",
        lambda **_kwargs: (fd, str(temporary)),
    )

    try:
        index_backfill_worker._save(
            tmp_path / "checkpoint.json",
            {"status": "safe"},
        )
    except OSError as exc:
        assert "temporary" in str(exc)
    else:
        raise AssertionError("A symlinked checkpoint temporary must be rejected")
    assert protected.read_text() == "do not truncate"
    assert not temporary.exists()


def test_checkpoint_creation_works_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(index_backfill_worker.os, "fchmod", raising=False)
    checkpoint = tmp_path / "windows-compatible.json"

    index_backfill_worker._save(checkpoint, {"status": "created"})

    assert json.loads(checkpoint.read_text()) == {"status": "created"}


def test_checkpoint_survives_ordinary_sqlite_writes(tmp_path):
    import sqlite3

    db = tmp_path / "market.sqlite3"
    checkpoint = tmp_path / "sp500.json"

    def acquire(_code, **kwargs):
        with sqlite3.connect(kwargs["db"]) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS observations (value TEXT)")
            conn.execute("INSERT INTO observations VALUES ('stored')")
        return {"status": "stored", "sessions": 1}

    args = {
        "code": "SP500",
        "state_path": checkpoint,
        "db": db,
        "start": date(2020, 1, 1),
        "end": date(2020, 1, 1),
        "once": True,
        "acquire": acquire,
    }
    assert index_backfill_worker.run(**args) == 0
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO observations VALUES ('later')")

    assert index_backfill_worker.run(**args) == 0
