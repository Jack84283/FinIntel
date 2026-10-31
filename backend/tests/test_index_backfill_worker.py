"""Offline tests for resumable bounded FRED index backfills."""

import json
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
