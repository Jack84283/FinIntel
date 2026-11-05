"""Detached SQLite acceptance evidence must be read-only and contain no raw rows."""

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "issue29_private_evidence.py"
SPEC = importlib.util.spec_from_file_location("issue29_private_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _fixture(path):
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 15")
        db.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, provider TEXT)")
        db.execute("CREATE TABLE daily_prices (series_id INTEGER, session_date TEXT, close TEXT)")
        db.execute(
            "CREATE TABLE index_daily_closes (index_code TEXT, provider TEXT, session_date TEXT)"
        )
        db.execute("CREATE TABLE data_quality_findings (id INTEGER, code TEXT, superseded_at TEXT)")
        db.execute("INSERT INTO series VALUES (1,'alpaca_sip')")
        db.execute("INSERT INTO daily_prices VALUES (1,'2026-11-01','private-price-1234')")
        db.execute("INSERT INTO index_daily_closes VALUES ('SP500','fred','2026-11-01')")
        db.execute("INSERT INTO data_quality_findings VALUES (1,'SP500',NULL)")


def test_detached_evidence_no_prices_or_paths(tmp_path, monkeypatch):
    copy = tmp_path / "private-copy.sqlite3"
    output = tmp_path / "evidence.json"
    _fixture(copy)
    before = copy.read_bytes()
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "live.sqlite3"))
    assert MODULE.main(["--inspect-db", str(copy), "--output", str(output), "--repeat", "2"]) == 0
    assert copy.read_bytes() == before
    report = json.loads(output.read_text())
    assert report["production_acceptance"] == "unverified"
    assert report["database"]["schema_version_matches"] is True
    assert report["checks"]["sqlite_quick_check"] == "not_run"
    assert all(q["status"] == "measured" for q in report["bounded_queries"])
    assert len(report["bounded_queries"]) == 3
    serialized = output.read_text()
    for secret in ("private-price-1234", "alpaca_sip", "fred", str(copy), str(tmp_path)):
        assert secret not in serialized
    if MODULE.os.name != "nt":
        assert output.stat().st_mode & 0o077 == 0


def test_integrity_opt_in_and_nonoverwrite(tmp_path):
    copy = tmp_path / "private-copy.sqlite3"
    _fixture(copy)
    output = tmp_path / "evidence.json"
    assert (
        MODULE.main(
            [
                "--inspect-db",
                str(copy),
                "--output",
                str(output),
                "--quick-check",
                "--foreign-key-check",
            ]
        )
        == 0
    )
    result = json.loads(output.read_text())
    assert result["checks"] == {
        "sqlite_quick_check": "pass",
        "foreign_key_check": "pass",
    }
    with pytest.raises(SystemExit):
        MODULE.main(["--inspect-db", str(copy), "--output", str(output)])


def test_active_db_and_sidecars_refused(tmp_path, monkeypatch):
    copy = tmp_path / "db.sqlite3"
    _fixture(copy)
    monkeypatch.setenv("FININTEL_DB_PATH", str(copy))
    with pytest.raises(ValueError, match="active"):
        MODULE.inspect(copy, repeats=1, sample=10, quick_check=False, foreign_keys=False)
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "active.sqlite3"))
    sidecar = Path(str(copy) + "-wal")
    sidecar.write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="sidecars"):
        MODULE.inspect(copy, repeats=1, sample=10, quick_check=False, foreign_keys=False)
    sidecar.unlink()
    link = tmp_path / "symlink.sqlite3"
    link.symlink_to(copy)
    with pytest.raises(ValueError, match="symlink"):
        MODULE.inspect(link, repeats=1, sample=10, quick_check=False, foreign_keys=False)


def test_incomplete_legacy_copy_does_not_crash(tmp_path, monkeypatch):
    copy = tmp_path / "old-copy.sqlite3"
    with sqlite3.connect(copy) as db:
        db.execute("PRAGMA user_version=14")
        db.execute("CREATE TABLE daily_prices (session_date TEXT)")
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "live.sqlite3"))
    report = MODULE.inspect(copy, repeats=2, sample=10, quick_check=False, foreign_keys=False)
    assert report["database"]["schema_version_matches"] is False
    assert "index_daily_closes" in report["database"]["missing_core_tables"]
    assert all(q["status"] == "not_run" for q in report["bounded_queries"])
    assert report["production_acceptance"] == "unverified"


def test_invalid_bounds_rejected_before_creating_report(tmp_path):
    copy = tmp_path / "copy.sqlite3"
    _fixture(copy)
    for args in (
        ["--repeat", "0"],
        ["--repeat", "11"],
        ["--sample", "0"],
        ["--sample", "501"],
    ):
        out = tmp_path / "evidence.json"
        with pytest.raises(SystemExit):
            MODULE.main(["--inspect-db", str(copy), "--output", str(out), *args])
        assert not out.exists()


def test_source_digests_are_stable_and_do_not_export_raw_rows(tmp_path, monkeypatch):
    copy = tmp_path / "detached.sqlite3"
    _fixture(copy)
    # Populate the columns needed for a deterministic full-observation digest.
    with sqlite3.connect(copy) as db:
        for column in ("open", "high", "low", "volume", "retrieved_at", "ingestion_id"):
            db.execute(f"ALTER TABLE daily_prices ADD COLUMN {column} TEXT")
        for column in ("close", "retrieved_at", "source_url"):
            db.execute(f"ALTER TABLE index_daily_closes ADD COLUMN {column} TEXT")
        for column in ("symbol", "frequency", "adjustment", "data_kind"):
            db.execute(f"ALTER TABLE series ADD COLUMN {column} TEXT")
        db.execute(
            "UPDATE series SET symbol='AAPL',frequency='1d',"
            "adjustment='raw_unadjusted',data_kind='historical_eod'"
        )
    remapped = tmp_path / "remapped.sqlite3"
    shutil.copyfile(copy, remapped)
    with sqlite3.connect(remapped) as db:
        db.execute("UPDATE series SET id=42")
        db.execute("UPDATE daily_prices SET series_id=42,ingestion_id='changed-local-id'")
    monkeypatch.setenv("FININTEL_DB_PATH", str(tmp_path / "live.sqlite3"))
    first = MODULE.inspect(
        copy, repeats=1, sample=10, quick_check=False, foreign_keys=False, source_digests=True
    )
    second = MODULE.inspect(
        remapped, repeats=1, sample=10, quick_check=False, foreign_keys=False, source_digests=True
    )
    # IDs differ after merge, but identical logical source observations hash identically.
    assert first["source_digests"] == second["source_digests"]
    assert first["source_digests"]["daily_prices"]["rows_hashed"] == 1
    assert first["source_digests"]["index_daily_closes"]["rows_hashed"] == 1
    assert "private-price-1234" not in json.dumps(first)
