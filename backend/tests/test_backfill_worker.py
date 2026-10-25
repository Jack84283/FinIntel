"""Offline tests for durable background backfill progress and daily request caps."""

import json

from finintel.market_data import backfill_worker
from finintel.market_data.provider import (
    ProviderAccessDenied,
    ProviderError,
    ProviderPayloadInvalid,
    ProviderRateLimited,
    ProviderSymbolUnavailable,
    ProviderUnavailable,
)


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


def test_unavailable_symbols_are_quarantined_and_dont_trip_circuit_breaker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(
        backfill_worker, "pending_symbols",
        lambda **kwargs: ["AAC-U", "AAC-WS", "AACB"],
    )
    calls = []

    def acquire(symbol, **kwargs):
        calls.append(symbol)
        if symbol in {"AAC-U", "AAC-WS"}:
            raise ProviderSymbolUnavailable("No bars")
        return {"import_id": 23}

    monkeypatch.setattr(backfill_worker, "acquire_and_store", acquire)
    checkpoint = tmp_path / "history.json"
    args = {
        "provider": "alpaca_sip",
        "asset_type": "all",
        "max_per_day": 0,
        "delay": 0,
        "state_path": checkpoint,
        "once": True,
    }
    assert backfill_worker.run(**args) == 0
    assert calls == ["AAC-U", "AAC-WS", "AACB"]
    state = json.loads(checkpoint.read_text())
    assert state["last_status"] == "retry_next_day"
    assert set(state["failed_symbols"]) == {"AAC-U", "AAC-WS"}
    assert state["failed_count"] == 2
    assert backfill_worker.run(**args) == 0
    assert len(calls) == 3

    # The explicit retry action rechecks only failed symbols on the same day.
    assert backfill_worker.run(**args, retry_failed=True) == 0
    assert calls[3:] == ["AAC-U", "AAC-WS"]


def test_all_unavailable_symbols_finish_with_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: ["AAC-U"])
    monkeypatch.setattr(
        backfill_worker, "acquire_and_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProviderSymbolUnavailable()),
    )
    checkpoint = tmp_path / "unavailable.json"
    assert backfill_worker.run(
        provider="alpaca_sip", asset_type="all", max_per_day=0,
        delay=0, state_path=checkpoint, once=False,
    ) == 0
    state = json.loads(checkpoint.read_text())
    assert state["last_status"] == "complete_with_failures"


def test_access_denied_pauses_and_preserves_retry_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: ["AAPL"])
    monkeypatch.setattr(
        backfill_worker, "acquire_and_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProviderAccessDenied()),
    )
    checkpoint = tmp_path / "access.json"
    assert backfill_worker.run(
        provider="alpaca_sip", asset_type="all", max_per_day=0,
        delay=0, state_path=checkpoint, once=True,
    ) == 2
    state = json.loads(checkpoint.read_text())
    assert state["last_status"] == "paused_provider_access"
    assert state["attempted"] == []
    assert "AAPL" not in state["failed_symbols"]


def test_three_transient_provider_errors_pause_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: ["AAPL"])
    monkeypatch.setattr(
        backfill_worker, "acquire_and_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProviderUnavailable()),
    )
    delays = []
    monkeypatch.setattr(backfill_worker.time, "sleep", delays.append)
    checkpoint = tmp_path / "network.json"
    assert backfill_worker.run(
        provider="alpaca_sip", asset_type="all", max_per_day=0,
        delay=0, state_path=checkpoint, once=False,
    ) == 2
    assert delays == [5, 10]
    state = json.loads(checkpoint.read_text())
    assert state["last_status"] == "paused_provider_unavailable"
    assert state["attempted"] == []


def test_three_symbol_errors_do_not_stop_bulk_download(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_worker, "alpaca_configured", lambda: True)
    symbols = ["AAC-U", "AAC-WS", "AACIW", "AACB"]
    monkeypatch.setattr(backfill_worker, "pending_symbols", lambda **kwargs: symbols)
    calls = []

    def acquire(symbol, **kwargs):
        calls.append(symbol)
        if symbol == "AACIW":
            raise ProviderPayloadInvalid("invalid payload")
        if symbol != "AACB":
            raise ProviderError("unsupported instrument")
        return {"import_id": 42}

    monkeypatch.setattr(backfill_worker, "acquire_and_store", acquire)
    state_path = tmp_path / "three-failures.json"
    result = backfill_worker.run(
        provider="alpaca_sip",
        asset_type="all",
        max_per_day=0,
        delay=0,
        state_path=state_path,
        once=True,
    )
    state = json.loads(state_path.read_text())
    assert result == 0
    assert calls == symbols
    assert state["last_status"] == "retry_next_day"
    assert state["failed_count"] == 3
    assert set(state["failed_symbols"]) == set(symbols[:-1])
