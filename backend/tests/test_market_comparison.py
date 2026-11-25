from contextlib import closing
from datetime import date, timedelta
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from finintel.main import app
from finintel.market_data.market_comparison import PATH
from finintel.storage.db import _write_connection


def equity(conn, symbol, provider, rows):
    retrieved = "2025-01-01T00:00:00Z"
    conn.execute("INSERT OR IGNORE INTO instruments VALUES (?,?)", (symbol, retrieved))
    sid = conn.execute(
        "INSERT INTO series(symbol,provider,frequency,adjustment,data_kind) "
        "VALUES (?,?,'1d','raw_unadjusted','historical')",
        (symbol, provider),
    ).lastrowid
    run = conn.execute(
        "INSERT INTO ingestion_runs(series_id,retrieved_at,imported_at,payload_sha256,"
        "source_url,provider_terms_url,first_session,last_session,observations,inserted,"
        "updated,unchanged,stale) VALUES (?,?,?,?,'https://example.test','https://example.test',?,?,?,?,0,0,0)",
        (sid, retrieved, retrieved, str(sid), rows[0][0], rows[-1][0], len(rows), len(rows)),
    ).lastrowid
    conn.executemany(
        "INSERT INTO daily_prices VALUES(?,?,?,?,?,?,?,?,?)",
        [(sid, day, close, close, close, close, 100, retrieved, run) for day, close in rows],
    )


def index(conn, code, rows):
    conn.executemany(
        "INSERT INTO index_daily_closes(index_code,provider,session_date,close,retrieved_at,source_url) "
        "VALUES (?,'fred',?,?,'2025-01-01T00:00:00Z','https://example.test')",
        [(code, day, close) for day, close in rows],
    )


AAPL = {"kind": "equity", "code": "AAPL", "provider": "tiingo"}
AAPL_OTHER = {**AAPL, "provider": "alpaca_sip"}
ETF = {"kind": "equity", "code": "QQQ", "provider": "alpha_vantage"}
SP500 = {"kind": "index", "code": "SP500", "provider": "fred"}
DATES = ["2024-01-02", "2024-01-03", "2024-01-05", "2024-01-08", "2024-01-09"]


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    monkeypatch.setenv("FININTEL_PROCESS_TOKEN", "synthetic")
    with closing(_write_connection(path)) as conn, conn:
        equity(conn, "AAPL", "tiingo", [(DATES[0], "98"), (DATES[1], "100.00"), (DATES[3], "105")])
        equity(conn, "AAPL", "alpaca_sip", [(DATES[1], "3"), (DATES[3], "4")])
        equity(conn, "QQQ", "alpha_vantage", [(DATES[1], "111"), (DATES[3], "115")])
        index(
            conn,
            "SP500",
            [(DATES[1], "5000"), (DATES[2], "5050"), (DATES[3], "5200"), (DATES[4], "5250")],
        )
    return path, TestClient(app, headers={"x-finintel-process-token": "synthetic"})


def payload(sources, **overrides):
    return {
        "contract_version": "market-comparison-v1",
        "sources": sources,
        "start": DATES[0],
        "end": DATES[-1],
        **overrides,
    }


def test_mixed_alignment_hashes_and_database_preservation(local, monkeypatch):
    path, client = local
    before = path.read_bytes()
    # The comparison never needs an external connection.
    monkeypatch.setattr(
        "socket.create_connection", lambda *_args, **_kwargs: pytest.fail("network attempted")
    )
    request = payload([AAPL, SP500, AAPL_OTHER, ETF])
    response = client.post(PATH, json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["request"] == request
    assert result["baseline_date"] == DATES[1]
    assert result["last_common_date"] == DATES[3]
    assert result["shared_sessions"] == 2
    assert [point["session_date"] for point in result["points"]] == DATES[1:]
    assert result["points"][0]["values"][0] == {
        "close": "100.00",
        "normalized_100": "100",
        "cumulative_return_pct": "0",
    }
    assert result["points"][1]["values"][0] is None
    assert result["points"][2]["values"][2]["cumulative_return_pct"] == "33.333333333333"
    assert result["points"][3]["values"][1]["normalized_100"] == "105"
    assert result["sources"][1]["common_end_return_pct"] == "4"
    assert result["sources"][1]["latest_close"] == "5250"
    assert result["sources"][0]["missing_sessions"] == 2
    assert result["sources"][0]["first_date"] == DATES[0]
    assert result["sources"][0]["snapshot_sha256"] != result["sources"][2]["snapshot_sha256"]
    assert result["sources"][1]["nonshared_sessions"] == 2
    assert "mixed_semantics" in result["warnings"]
    assert client.post(PATH, json=request).json() == result
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("sources", "overrides", "error"),
    [
        ([AAPL, AAPL], {}, "invalid_input"),
        ([AAPL], {}, "invalid_input"),
        ([AAPL, {**SP500, "code": "VIX"}], {}, "unsupported_vix"),
        ([AAPL, {**SP500, "provider": "tiingo"}], {}, "invalid_source"),
        ([AAPL, {**AAPL, "provider": "alpha_vantage"}], {}, "missing_history"),
        ([AAPL, SP500], {"end": DATES[2]}, "insufficient_overlap"),
        ([AAPL, SP500], {"start": "2010-01-01"}, "resource_limit"),
        ([AAPL, SP500], {"start": "2024-02-30"}, "invalid_input"),
    ],
)
def test_refuses_unsupported_or_insufficient_data(local, sources, overrides, error):
    _, client = local
    response = client.post(PATH, json=payload(sources, **overrides))
    assert response.status_code == 422
    assert response.json() == {"detail": error}


def test_managed_authentication_and_payload_bound(local):
    _, client = local
    assert TestClient(app).post(PATH, json=payload([AAPL, SP500])).status_code == 403
    assert client.post(PATH, content=" " * 4097).json() == {"detail": "resource_limit"}


@pytest.mark.parametrize("bad", ["0", "-1", "Infinity", "NaN", "1e-999"])
def test_bad_closes_fail_closed(local, bad):
    path, client = local
    with closing(_write_connection(path)) as conn, conn:
        conn.execute("UPDATE index_daily_closes SET close=? WHERE session_date=?", (bad, DATES[1]))
    assert client.post(PATH, json=payload([AAPL, SP500])).json() == {"detail": "invalid_close"}


def test_ten_year_four_source_fixture(tmp_path, monkeypatch):
    path = tmp_path / "scale.sqlite3"
    monkeypatch.setenv("FININTEL_DB_PATH", str(path))
    start, end = date(2014, 1, 1), date(2024, 1, 1)
    rows = [
        (day.isoformat(), str(300 + i))
        for i in range((end - start).days + 1)
        if (day := start + timedelta(days=i)).weekday() < 5
    ]
    with closing(_write_connection(path)) as conn, conn:
        equity(conn, "AAPL", "tiingo", rows)
        equity(conn, "AAPL", "alpaca_sip", rows)
        equity(conn, "QQQ", "alpha_vantage", rows)
        index(conn, "SP500", rows)
    begin = perf_counter()
    response = TestClient(app).post(
        PATH,
        json=payload(
            [AAPL, SP500, AAPL_OTHER, ETF],
            start=start.isoformat(),
            end=end.isoformat(),
        ),
    )
    elapsed = perf_counter() - begin
    assert response.status_code == 200, response.text
    assert len(response.json()["points"]) == len(rows)
    assert len(response.content) < 4 * 1024 * 1024
    print(
        f"10-year fixture: {len(rows)} sessions x 4; {len(response.content)} bytes; {elapsed:.3f}s"
    )


def test_equity_only_sources_and_single_source_semantic_refusal(local):
    path, client = local
    result = client.post(PATH, json=payload([AAPL, ETF])).json()
    assert result["shared_sessions"] == 2
    assert result["sources"][1]["identity"] == ETF
    assert "mixed_semantics" not in result["warnings"]
    assert all(source["adjustment"] == "raw_unadjusted" for source in result["sources"])
    with closing(_write_connection(path)) as conn, conn:
        conn.execute("UPDATE series SET adjustment='adjusted' WHERE symbol='QQQ'")
    response = client.post(PATH, json=payload([AAPL, ETF]))
    assert response.json() == {"detail": "incompatible_source"}


def test_no_common_session_or_provider_substitution(local):
    path, client = local
    with closing(_write_connection(path)) as conn, conn:
        index(conn, "NDX", [(DATES[2], "5100"), (DATES[-1], "5200")])
    request = payload([AAPL, {**SP500, "code": "NDX"}])
    response = client.post(PATH, json=request)
    assert response.json() == {"detail": "insufficient_overlap"}
