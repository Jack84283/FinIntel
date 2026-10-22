"""SQLite durability, versioning, idempotence, corruption, and read semantics."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from finintel.market_data.models import HistoricalBatch
from finintel.market_data.provider import parse_alpha_vantage_daily
from finintel.storage.db import (
    StorageError,
    import_snapshot,
    list_imports,
    list_instruments,
    read_daily,
)

FIXTURE = Path(__file__).parent / "fixtures" / "alpha_vantage_daily_SYNTHETIC.json"
SOURCE = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM"


def snapshot_at(tmp_path, *, shift_days=0, changes=None, filename="snapshot.json"):
    raw = json.loads(FIXTURE.read_text())
    if changes:
        changes(raw)
    batch = parse_alpha_vantage_daily(
        json.dumps(raw).encode(), symbol="IBM", source_url=SOURCE
    )
    if shift_days:
        batch = batch.model_copy(
            update={"retrieved_at": batch.retrieved_at + timedelta(days=shift_days)}
        )
    target = tmp_path / filename
    target.write_text(batch.model_dump_json(indent=2))
    return target


def test_import_and_idempotent_reimport(tmp_path):
    db = tmp_path / "db.sqlite3"
    snapshot = snapshot_at(tmp_path)
    first = import_snapshot(snapshot, db=db)
    second = import_snapshot(snapshot, db=db)
    assert first["inserted"] == 3
    assert first["updated"] == 0
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 3
    assert len(list_instruments(db)) == 1
    catalog = list_instruments(db)[0]
    assert catalog["sessions"] == 3
    assert catalog["first_session"] == "2026-10-16"
    assert catalog["last_session"] == "2026-10-20"
    response = read_daily("ibm", db=db)
    assert response is not None
    assert response["price_adjustment"] == "raw_unadjusted"
    assert response["provider"] == "alpha_vantage"
    assert response["observations"][0]["close"] == "98.50"
    assert response["observations"][-1]["close"] == "104.10"
    assert "apikey" not in response["source_url"]
    assert len(list_imports(db=db)) == 2


def test_updates_and_stale_snapshot_does_not_overwrite(tmp_path):
    db = tmp_path / "db.sqlite3"
    base = snapshot_at(tmp_path, filename="old.json")
    import_snapshot(base, db=db)
    changed = snapshot_at(
        tmp_path,
        shift_days=1,
        changes=lambda p: p["Time Series (Daily)"]["2026-10-20"].update(
            {"4. close": "105.00"}
        ),
        filename="new.json",
    )
    result = import_snapshot(changed, db=db)
    assert result["updated"] == 1
    assert result["unchanged"] == 2
    assert read_daily("IBM", db=db)["observations"][-1]["close"] == "105.00"
    # Older snapshot cannot roll back a revised recent price.
    stale = import_snapshot(base, db=db)
    assert stale["stale"] == 1
    assert stale["updated"] == 0
    assert read_daily("IBM", db=db)["observations"][-1]["close"] == "105.00"


def test_invalid_snapshot_is_not_committed(tmp_path):
    db = tmp_path / "db.sqlite3"
    good = snapshot_at(tmp_path)
    import_snapshot(good, db=db)
    rows_before = read_daily("IBM", db=db)["observations"]
    runs_before = len(list_imports(db=db))
    broken = tmp_path / "bad.json"
    invalid = json.loads(good.read_text())
    invalid["observations"][0]["volume"] = -1
    broken.write_text(json.dumps(invalid))
    with pytest.raises(StorageError, match="validation"):
        import_snapshot(broken, db=db)
    assert read_daily("IBM", db=db)["observations"] == rows_before
    assert len(list_imports(db=db)) == runs_before


def test_validation_error_before_creating_db(tmp_path):
    db = tmp_path / "db.sqlite3"
    source = tmp_path / "invalid.json"
    source.write_text("{}")
    with pytest.raises(StorageError):
        import_snapshot(source, db=db)
    assert not db.exists()


def test_read_only_queries_do_not_create_db(tmp_path):
    db = tmp_path / "absent.sqlite3"
    assert list_instruments(db) == []
    assert read_daily("IBM", db=db) is None
    assert list_imports(db=db) == []
    assert not db.exists()


def test_date_filter_and_page_limit(tmp_path):
    db = tmp_path / "db.sqlite3"
    import_snapshot(snapshot_at(tmp_path), db=db)
    from datetime import date

    limited = read_daily("IBM", db=db, limit=1)
    assert limited["has_more"] is True
    assert limited["observations"][0]["session_date"] == "2026-10-16"
    filtered = read_daily(
        "IBM", db=db, start=date(2026, 10, 19), end=date(2026, 10, 20)
    )
    assert len(filtered["observations"]) == 2
    assert filtered["has_more"] is False


def test_newer_schema_refused_without_mutation(tmp_path):
    db = tmp_path / "db.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(StorageError, match="newer schema"):
        import_snapshot(snapshot_at(tmp_path), db=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_unversioned_database_refused_without_destroying_data(tmp_path):
    db = tmp_path / "db.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE legacy_keep (important TEXT)")
        conn.execute("INSERT INTO legacy_keep VALUES ('safe')")
    with pytest.raises(StorageError, match="unversioned"):
        import_snapshot(snapshot_at(tmp_path), db=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT important FROM legacy_keep").fetchone()[0] == "safe"


def test_snapshot_source_requires_sanitized_url(tmp_path):
    snapshot = snapshot_at(tmp_path)
    data = json.loads(snapshot.read_text())
    data["source_url"] += "&apikey=SECRET_NOT_A_REAL_KEY"
    snapshot.write_text(json.dumps(data))
    db = tmp_path / "db.sqlite3"
    with pytest.raises(StorageError, match="sanitized"):
        import_snapshot(snapshot, db=db)
    assert not db.exists()


def test_import_does_not_write_float_prices(tmp_path):
    db = tmp_path / "db.sqlite3"
    import_snapshot(snapshot_at(tmp_path), db=db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT typeof(open), typeof(close) FROM daily_prices").fetchall()
        assert rows == [("text", "text")] * 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_revalidated_snapshot_has_consistent_contract(tmp_path):
    snap = snapshot_at(tmp_path)
    batch = HistoricalBatch.model_validate_json(snap.read_bytes())
    assert batch.first_session.isoformat() == "2026-10-16"
    assert batch.observations[-1].close.as_tuple().exponent == -2
