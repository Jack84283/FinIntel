"""Offline tests for durable background backfill progress and daily request caps."""

import json

from finintel.market_data import backfill_worker
from finintel.market_data.provider import ProviderRateLimited


def test_backfill_checkpoint_prevents_duplicate_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: ["AAPL", "MSFT"])
    calls = []

    def acquire(symbol, **kwargs):
        calls.append(symbol)
        return {"import_id": len(calls)}

    monkeypatch.setattr(backfill_worker, "acquire_and_store", acquire)
    checkpoint = tmp_path / "progress.json"
    args = {
        "provider": "alpaca_sip",
        "asset_type": "Stock",
        "max_per_day": 2,
        "delay": 0,
        "state_path": checkpoint,
        "once": True,
    }
    assert backfill_worker.run(**args) == 0
    assert calls == ["AAPL", "MSFT"]
    assert backfill_worker.run(**args) == 0
    assert calls == ["AAPL", "MSFT"]
    saved = json.loads(checkpoint.read_text())
    assert saved["requests"] == 2
    assert set(saved["attempted"]) == {"AAPL", "MSFT"}


def test_backfill_rejects_provider_change_for_checkpoint(tmp_path):
    checkpoint = tmp_path / "progress.json"
    checkpoint.write_text(
        json.dumps(
            {
                "provider": "alpha_vantage",
                "asset_type": "Stock",
                "day": "2026-10-23",
                "attempted": [],
                "requests": 0,
            }
        )
    )
    try:
        backfill_worker._load(checkpoint, provider="alpaca_sip", asset_type="Stock")
    except ValueError:
        pass
    else:
        raise AssertionError("Provider mismatch must fail safely")


def test_unlimited_backfill_ignores_daily_safety_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: ["A", "AA"])
    calls = []

    def acquire(symbol, **kwargs):
        calls.append(symbol)
        return {"import_id": len(calls)}

    monkeypatch.setattr(backfill_worker, "acquire_and_store", acquire)
    checkpoint = tmp_path / "unlimited.json"
    assert (
        backfill_worker.run(
            provider="alpaca_sip",
            asset_type="all",
            max_per_day=0,
            delay=0,
            state_path=checkpoint,
            once=True,
        )
        == 0
    )
    assert calls == ["A", "AA"]
    assert json.loads(checkpoint.read_text())["requests"] == 2


def test_rate_limit_backs_off_and_retries_without_waiting_for_midnight(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    complete = []
    monkeypatch.setattr(
        backfill_worker,
        "pending_symbols",
        lambda **kwargs: [] if complete else ["AAPL"],
    )
    waits = []
    calls = []

    def acquire(symbol, **kwargs):
        calls.append(symbol)
        if len(calls) == 1:
            raise ProviderRateLimited("Simulated quota")
        complete.append(symbol)
        return {"import_id": 42}

    monkeypatch.setattr(backfill_worker, "acquire_and_store", acquire)
    monkeypatch.setattr(backfill_worker.time, "sleep", waits.append)
    monkeypatch.setattr(
        backfill_worker,
        "_sleep_until_next_utc_day",
        lambda: (_ for _ in ()).throw(AssertionError("Must not wait until tomorrow")),
    )
    checkpoint = tmp_path / "rate-limit.json"
    assert (
        backfill_worker.run(
            provider="alpaca_sip",
            asset_type="all",
            max_per_day=0,
            delay=0,
            state_path=checkpoint,
            once=False,
        )
        == 0
    )
    assert calls == ["AAPL", "AAPL"]
    assert waits == [60]
    saved = json.loads(checkpoint.read_text())
    assert saved["requests"] == 2
    assert saved["last_status"] == "complete"
