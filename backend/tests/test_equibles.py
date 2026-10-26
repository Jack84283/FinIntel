"""Equibles download is paginated, complete and never writes partial snapshots."""

import pytest

from finintel.market_data import equibles


def test_acquisition_paginates_and_commits_once(monkeypatch):
    data = [
        {"ticker": "AAPL", "name": "Apple Inc.", "isLinked": True},
        {"ticker": "MSFT", "name": "Microsoft Corp", "isLinked": True},
    ]
    seen = []
    saved = []

    def fake_fetch(code, offset):
        seen.append((code, offset))
        page = data[offset : offset + 1]
        return {
            "slug": "sp-500",
            "asOfDate": "2026-10-23",
            "constituentCount": 2,
            "unlinkedCount": 0,
            "data": page,
            "meta": {"offset": offset, "total": 2, "count": len(page), "hasMore": offset == 0},
        }

    def fake_load(*args, **kwargs):
        saved.append((args, kwargs))
        return 2

    monkeypatch.setattr(equibles, "_fetch", fake_fetch)
    monkeypatch.setattr(equibles, "load_membership", fake_load)
    result = equibles.acquire_index("SP500")
    assert seen == [("SP500", 0), ("SP500", 1)]
    assert saved[0][0][3] == ["AAPL", "MSFT"]
    assert saved[0][1]["expected_count"] == 2
    assert saved[0][1]["names"]["AAPL"] == "Apple Inc."
    assert result["provenance"] == "fund_derived_proxy"


def test_incomplete_acquisition_never_writes(monkeypatch):
    def fake_fetch(code, offset):
        return {
            "slug": "sp-500",
            "asOfDate": "2026-10-23",
            "constituentCount": 2,
            "unlinkedCount": 0,
            "data": [{"ticker": "AAPL", "isLinked": True}],
            "meta": {"offset": 0, "count": 1, "hasMore": False, "total": 2},
        }

    monkeypatch.setattr(equibles, "_fetch", fake_fetch)
    monkeypatch.setattr(
        equibles, "load_membership", lambda *a, **k: pytest.fail("wrote partial data")
    )
    with pytest.raises(equibles.EquiblesError, match="incomplete"):
        equibles.acquire_index("SP500")


def test_unlinked_holding_is_reported_without_fake_ticker(monkeypatch):
    monkeypatch.setattr(
        equibles,
        "_fetch",
        lambda code, offset: {
            "slug": "sp-500",
            "asOfDate": "2026-10-23",
            "constituentCount": 2,
            "unlinkedCount": 1,
            "data": [
                {"ticker": "AAPL", "name": "Apple Inc.", "isLinked": True},
                {"ticker": None, "name": "Unmatched security", "isLinked": False},
            ],
            "meta": {"offset": 0, "count": 2, "hasMore": False, "total": 2},
        },
    )
    saved = []
    monkeypatch.setattr(equibles, "load_membership", lambda *a, **k: saved.append((a, k)) or 1)
    result = equibles.acquire_index("SP500")
    assert saved[0][0][3] == ["AAPL"]
    assert saved[0][1]["expected_count"] == 1
    assert result["complete"] is False
    assert result["unlinked_count"] == 1
    assert "incomplete" in result["source"]
