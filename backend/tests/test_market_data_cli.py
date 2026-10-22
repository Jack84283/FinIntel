"""Atomic local snapshots. Network is mocked; no live API traffic in CI."""

import json
from pathlib import Path

from finintel.market_data import HistoricalBatch, ProviderUnavailable
from finintel.market_data.__main__ import main, write_snapshot
from finintel.market_data.provider import parse_alpha_vantage_daily

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"


def fixture_batch() -> HistoricalBatch:
    return parse_alpha_vantage_daily(
        FIXTURE.read_bytes(),
        symbol="IBM",
        source_url="https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM",
    )


def test_snapshot_is_valid_json_and_atomic(tmp_path):
    target = tmp_path / "data" / "raw" / "IBM.json"
    write_snapshot(target, fixture_batch())
    data = json.loads(target.read_text())
    assert data["provider"] == "alpha_vantage"
    assert data["data_kind"] == "historical_eod"
    assert data["observations"][0]["session_date"] == "2026-10-16"
    assert data["observations"][-1]["close"] == "104.10"
    assert list(target.parent.glob("*.tmp")) == []


def test_snapshot_refuses_symlink_overwrite(tmp_path):
    import pytest

    original = tmp_path / "original.txt"
    original.write_text("KEEP")
    link = tmp_path / "IBM.json"
    link.symlink_to(original)
    with pytest.raises(OSError, match="symlink"):
        write_snapshot(link, fixture_batch())
    assert original.read_text() == "KEEP"


def test_failed_cli_request_preserves_previous_snapshot(tmp_path, monkeypatch, capsys):
    target = tmp_path / "IBM.json"
    write_snapshot(target, fixture_batch())
    before = target.read_bytes()

    class Failed:
        def fetch_daily(self, symbol, *, outputsize="compact"):
            raise ProviderUnavailable("Provider unavailable")

    monkeypatch.setattr("finintel.market_data.__main__.AlphaVantageDailyProvider", lambda: Failed())
    assert main(["--symbol", "IBM", "--output", str(target)]) == 1
    assert target.read_bytes() == before
    assert "Provider unavailable" in capsys.readouterr().err


def test_successful_cli_request_summarizes_without_secrets(tmp_path, monkeypatch, capsys):
    target = tmp_path / "IBM.json"

    class Mocked:
        def fetch_daily(self, symbol, *, outputsize="compact"):
            assert symbol == "IBM"
            assert outputsize == "compact"
            return fixture_batch()

    monkeypatch.setattr("finintel.market_data.__main__.AlphaVantageDailyProvider", lambda: Mocked())
    assert main(["--symbol", "IBM", "--output", str(target)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "validated_local_snapshot"
    assert summary["observations"] == 3
    assert summary["not_live_quotes"] is True
    assert target.exists()
