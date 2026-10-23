"""Offline tests for durable background backfill progress and daily request caps."""

import json

from finintel.market_data import backfill_worker


def test_backfill_checkpoint_prevents_duplicate_requests(tmp_path, monkeypatch):
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
