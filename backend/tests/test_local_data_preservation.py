"""Safety tests for preserving ignored local artifacts outside the checkout."""

import json
import sqlite3
import subprocess

import pytest
from scripts.preserve_local_data import preserve, restore, verify


def test_preservation_manifest_round_trip_uses_a_safe_database_copy(tmp_path, monkeypatch):
    root = tmp_path / "FinIntel"
    (root / "data" / "backups").mkdir(parents=True)
    (root / "data" / "indexes").mkdir()
    (root / "data" / "indexes" / "sp500.csv").write_text("symbol\nAAPL\n")
    database = root / "data" / "finintel.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE prices (symbol TEXT, close TEXT)")
        conn.execute("INSERT INTO prices VALUES ('AAPL', '100')")
    snapshot = root / "data" / "backups" / "finintel-manual.sqlite3"
    snapshot.write_bytes(b"backup")
    report = root / "data" / "market-validation-report.json"
    report.write_text('{"status":"review"}', encoding="utf-8")

    def fake_git(*args, **kwargs):
        if "--cached" in args[0]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=b"data/indexes/sp500.csv\0"
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                b"data/finintel.sqlite3\0"
                b"data/backups/finintel-manual.sqlite3\0"
                b"data/market-validation-report.json\0"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_git)
    destination = tmp_path / "preservation"
    manifest = preserve(root, destination)
    verify(manifest)

    with sqlite3.connect(destination / "data/finintel.sqlite3") as conn:
        assert conn.execute("SELECT * FROM prices").fetchone() == ("AAPL", "100")
    assert json.loads((destination / "data/market-validation-report.json").read_text()) == {
        "status": "review"
    }

    report.unlink()
    database.unlink()
    snapshot.unlink()
    restore(manifest, root)
    assert report.read_text(encoding="utf-8") == '{"status":"review"}'


def test_restore_refuses_to_overwrite_divergent_local_data(tmp_path, monkeypatch):
    root = tmp_path / "FinIntel"
    (root / "data").mkdir(parents=True)
    report = root / "data" / "report.json"
    report.write_text('{"status":"original"}', encoding="utf-8")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=0, stdout=b"data/report.json\0"
        ),
    )
    manifest = preserve(root, tmp_path / "preservation")
    report.write_text('{"status":"changed"}', encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        restore(manifest, root)
