"""Run a disposable daily-acquisition acceptance check.

The default mode is synthetic and never contacts a provider.  Live checks are
opt-in with ``--mode live`` and always run against a new SQLite path.  A live
run reports unavailable credentials, quotas, and provider outages as
``blocked`` instead of presenting them as passing results.

Examples::

    python scripts/daily_acquisition_acceptance.py
    python scripts/daily_acquisition_acceptance.py --mode live \
        --provider alpha_vantage --provider tiingo --symbol AAPL \
        --index SP500 --output /private/finintel-acceptance.json

Exit codes are 0 for pass, 2 when manual review is required (blocked or
not-run checks), and 1 for a failed check or an unsafe database path.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYMBOL = "AAPL"
DEFAULT_INDEX = "SP500"
EQUITY_PROVIDERS = ("alpaca_sip", "tiingo", "alpha_vantage")
INDEX_CODES = ("SP500", "DJIA", "NASDAQCOM", "NDX", "VIX")
DESKTOP_PLATFORMS = ("macos", "windows", "linux")
ACCEPTANCE_STATUSES = ("pass", "fail", "blocked", "not_run")
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
REPORT_VERSION = 1


def _redact_message(value: object) -> str:
    """Keep provider diagnostics useful without allowing credentials in reports."""
    message = str(value)
    message = re.sub(
        r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)", r"\1=<redacted>", message
    )
    message = re.sub(r"https?://[^\s?]+\?[^\s]+", "<provider-url-redacted>", message)
    return message[:500]


def _check(
    check_id: str,
    status: str,
    summary: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    if status not in ("pass", "fail", "blocked", "not_run"):
        raise ValueError("Invalid acceptance status")
    return {
        "id": check_id,
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
    }


def _checks_verdict(checks: list[dict[str, object]]) -> str:
    statuses = [check["status"] for check in checks]
    if "fail" in statuses:
        return "fail"
    if any(status in ("blocked", "not_run") for status in statuses):
        return "review"
    return "pass"


def _acceptance_matrix(
    live_results: dict[str, dict[str, object]],
    desktop_results: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return every required production check, including omitted checks."""
    desktop_results = desktop_results or {}
    matrix: list[dict[str, object]] = []
    for provider in (*EQUITY_PROVIDERS, "fred"):
        result = live_results.get(provider)
        matrix.append(
            {
                "id": "provider." + provider,
                "category": "real_provider",
                "status": result["status"] if result else "not_run",
                "summary": (
                    result["summary"]
                    if result
                    else "Provider was not selected for this acceptance run."
                ),
                "evidence": result.get("evidence", {}) if result else {},
            }
        )
    for platform_name in DESKTOP_PLATFORMS:
        result = desktop_results.get(platform_name)
        if result:
            result = _validate_desktop_result(platform_name, result)
        matrix.append(
            {
                "id": "desktop." + platform_name,
                "category": "packaged_desktop",
                "status": result["status"] if result else "not_run",
                "summary": (
                    result["summary"]
                    if result
                    else "No clean installed-desktop evidence was supplied."
                ),
                "evidence": result.get("evidence", {}) if result else {},
            }
        )
    return matrix


def _validate_desktop_result(
    platform_name: str, value: dict[str, object]
) -> dict[str, object]:
    """Validate one desktop result, including required proof for a pass."""
    status = value.get("status")
    if status not in ACCEPTANCE_STATUSES:
        raise ValueError(
            f"Desktop evidence for {platform_name} must have a valid status"
        )
    summary = value.get("summary")
    if summary is None:
        summary = f"Manual packaged-desktop check recorded as {status}."
    if not isinstance(summary, str):
        raise TypeError(f"Desktop evidence summary for {platform_name} must be text")
    details = value.get("evidence", {})
    if not isinstance(details, dict):
        raise TypeError(
            f"Desktop evidence details for {platform_name} must be an object"
        )
    if status == "pass":
        tested_at = details.get("tested_at")
        if not isinstance(tested_at, str) or not tested_at.strip():
            raise ValueError(
                f"Passing {platform_name} evidence requires a non-empty tested_at timestamp"
            )
        try:
            parsed_timestamp = datetime.fromisoformat(tested_at)
        except ValueError as exc:
            raise ValueError(
                f"Passing {platform_name} evidence requires an ISO-8601 tested_at timestamp"
            ) from exc
        if parsed_timestamp.tzinfo is None:
            raise ValueError(
                f"Passing {platform_name} evidence requires a timezone-aware tested_at timestamp"
            )

        build_id = details.get("build_id")
        if not isinstance(build_id, str) or not build_id.strip():
            raise ValueError(
                f"Passing {platform_name} evidence requires a non-empty build_id"
            )

        tested_features = details.get("tested_features")
        if (
            not isinstance(tested_features, list)
            or not tested_features
            or any(
                not isinstance(feature, str) or not feature.strip()
                for feature in tested_features
            )
        ):
            raise ValueError(
                f"Passing {platform_name} evidence requires a non-empty tested_features list"
            )
    return {"status": status, "summary": summary, "evidence": details}


def _load_desktop_evidence(path: Path) -> dict[str, dict[str, object]]:
    """Load and validate manually recorded packaged-desktop acceptance evidence."""
    with path.expanduser().resolve().open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise TypeError("Desktop evidence must be a JSON object keyed by platform")

    evidence: dict[str, dict[str, object]] = {}
    for platform_name, value in payload.items():
        if platform_name not in DESKTOP_PLATFORMS:
            raise ValueError(f"Unknown desktop platform in evidence: {platform_name}")
        if not isinstance(value, dict):
            raise TypeError(f"Desktop evidence for {platform_name} must be an object")
        evidence[platform_name] = _validate_desktop_result(platform_name, value)
    return evidence


def _apply_desktop_evidence(
    report: dict[str, object],
    desktop_results: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Merge manual desktop results into an existing live acceptance report."""
    run = report.get("run")
    production = report.get("production_acceptance")
    matrix = report.get("acceptance_matrix")
    if not isinstance(run, dict) or run.get("mode") != "live":
        raise ValueError("Desktop evidence can only be recorded on a live report")
    if not isinstance(production, dict) or not isinstance(matrix, list):
        raise TypeError("Acceptance report has no production acceptance matrix")

    updated = False
    for item in matrix:
        if not isinstance(item, dict) or item.get("category") != "packaged_desktop":
            continue
        platform_name = str(item.get("id", "")).removeprefix("desktop.")
        result = desktop_results.get(platform_name)
        if result is None:
            continue
        result = _validate_desktop_result(platform_name, result)
        item.update(
            status=result["status"],
            summary=result["summary"],
            evidence=result.get("evidence", {}),
        )
        updated = True
    if not updated:
        raise ValueError("Desktop evidence did not match a required platform")

    production["matrix"] = matrix
    production["verdict"] = _checks_verdict(list(production.get("checks", [])) + matrix)
    report["acceptance_matrix"] = matrix
    synthetic_acceptance = report.get("synthetic_acceptance")
    synthetic_verdict = (
        synthetic_acceptance.get("verdict", "review")
        if isinstance(synthetic_acceptance, dict)
        else "review"
    )
    report["verdict"] = _overall_verdict(
        "live", synthetic_verdict, production["verdict"]
    )
    recorded_at = datetime.now(UTC).isoformat(timespec="seconds")
    run["desktop_evidence_recorded_at"] = recorded_at
    run["finished_at"] = recorded_at
    return report


def _report_exit_code(report: dict[str, object]) -> int:
    return {
        "synthetic_pass": 0,
        "synthetic_review": 2,
        "synthetic_fail": 1,
        "pass": 0,
        "review": 2,
        "fail": 1,
        "not_run": 2,
    }[str(report["verdict"])]


def _overall_verdict(mode: str, synthetic_verdict: str, production_verdict: str) -> str:
    if mode != "live":
        return "synthetic_" + synthetic_verdict
    if "fail" in (synthetic_verdict, production_verdict):
        return "fail"
    if synthetic_verdict in ("review", "not_run") or production_verdict in (
        "review",
        "not_run",
    ):
        return "review"
    return "pass"


def _database_path(value: Path | None) -> tuple[Path, bool]:
    """Return a new database path and whether the harness owns its directory."""
    if value is None:
        directory = Path(tempfile.mkdtemp(prefix="finintel-daily-acceptance-"))
        return directory / "finintel.sqlite3", True
    candidate = value.expanduser().resolve()
    normal = (ROOT / "data" / "finintel.sqlite3").resolve()
    if candidate == normal:
        raise ValueError("Refusing to use the normal FinIntel database for acceptance")
    if candidate.exists():
        raise ValueError("Acceptance --db-path must not already exist")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate, False


def _known_database_paths() -> set[Path]:
    paths = {(ROOT / "data" / "finintel.sqlite3").resolve()}
    configured = os.environ.get("FININTEL_DB_PATH")
    if configured:
        paths.add(Path(configured).expanduser().resolve())
    for directory in (ROOT / "data", ROOT / "data" / "backups"):
        if directory.is_dir():
            for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.db-*", "*.sqlite*"):
                paths.update(item.resolve() for item in directory.glob(pattern))
    return _with_sqlite_sidecars(paths)


def _with_sqlite_sidecars(database_paths: set[Path]) -> set[Path]:
    return database_paths | {
        Path(str(database_path) + suffix)
        for database_path in database_paths
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }


def _validate_output_path(output: Path, database_paths: set[Path]) -> Path:
    candidate = output.expanduser().resolve()
    database_suffixes = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".journal"}
    compound_sidecar = re.search(
        r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$",
        candidate.name,
        flags=re.IGNORECASE,
    )
    protected_paths = _with_sqlite_sidecars(database_paths)
    if (
        candidate in protected_paths
        or candidate.suffix.lower() in database_suffixes
        or compound_sidecar
    ):
        raise ValueError("Refusing to write an acceptance report over a SQLite path")
    if candidate.exists() and candidate.is_file():
        with candidate.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\x00":
                raise ValueError(
                    "Refusing to replace an existing SQLite database with a report"
                )
    return candidate


def _atomic_write_report(output: Path, report: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix="." + output.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(report, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


@contextmanager
def _disposable_database(value: Path | None) -> Iterator[Path]:
    path, owns_directory = _database_path(value)
    previous = os.environ.get("FININTEL_DB_PATH")
    previous_token = os.environ.pop("FININTEL_PROCESS_TOKEN", None)
    os.environ["FININTEL_DB_PATH"] = str(path)
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("FININTEL_DB_PATH", None)
        else:
            os.environ["FININTEL_DB_PATH"] = previous
        if previous_token is not None:
            os.environ["FININTEL_PROCESS_TOKEN"] = previous_token
        if owns_directory:
            for generated in path.parent.glob("*"):
                generated.unlink(missing_ok=True)
            path.parent.rmdir()


def _synthetic_batch():
    from decimal import Decimal

    from finintel.market_data.models import DailyOHLCV, HistoricalBatch

    observations = [
        DailyOHLCV(
            session_date=date(2026, 10, 27) + timedelta(days=offset),
            open=Decimal(100) + offset,
            high=Decimal(101) + offset,
            low=Decimal(99) + offset,
            close=Decimal("100.5") + offset,
            volume=1000 + offset,
        )
        for offset in range(3)
    ]
    return HistoricalBatch(
        provider="alpha_vantage",
        symbol=DEFAULT_SYMBOL,
        source_url=(
            "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&"
            "symbol=AAPL&outputsize=compact&datatype=json"
        ),
        provider_terms_url="https://www.alphavantage.co/terms_of_service/",
        provider_last_refreshed="2026-10-29",
        retrieved_at=datetime(2026, 10, 30, 12, tzinfo=UTC),
        observations=observations,
    )


def _sqlite_evidence(db: Path) -> dict[str, object]:
    if not db.is_file():
        return {
            "integrity_check": "not_run",
            "duplicate_price_keys": 0,
            "duplicate_index_keys": 0,
        }
    with sqlite3.connect(db) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        duplicate_prices = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT series_id, session_date FROM daily_prices "
            "GROUP BY series_id, session_date HAVING COUNT(*) > 1"
            ")"
        ).fetchone()[0]
        duplicate_indexes = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT index_code, provider, session_date FROM index_daily_closes "
            "GROUP BY index_code, provider, session_date HAVING COUNT(*) > 1"
            ")"
        ).fetchone()[0]
        return {
            "integrity_check": integrity,
            "duplicate_price_keys": duplicate_prices,
            "duplicate_index_keys": duplicate_indexes,
        }


def _coverage_evidence(db: Path) -> dict[str, object]:
    if not db.is_file():
        return {"equity": [], "indexes": []}
    with sqlite3.connect(db) as connection:
        equity = [
            dict(
                zip(
                    ("symbol", "provider", "sessions", "first_session", "last_session"),
                    row,
                )
            )
            for row in connection.execute(
                "SELECT s.symbol,s.provider,COUNT(*),MIN(p.session_date),MAX(p.session_date) "
                "FROM series s JOIN daily_prices p ON p.series_id=s.id "
                "GROUP BY s.symbol,s.provider ORDER BY s.symbol,s.provider"
            )
        ]
        indexes = [
            dict(
                zip(
                    ("code", "provider", "sessions", "first_session", "last_session"),
                    row,
                )
            )
            for row in connection.execute(
                "SELECT index_code,provider,COUNT(*),MIN(session_date),MAX(session_date) "
                "FROM index_daily_closes GROUP BY index_code,provider "
                "ORDER BY index_code,provider"
            )
        ]
    return {"equity": equity, "indexes": indexes}


def _market_data_counts(db: Path) -> dict[str, int]:
    if not db.is_file():
        return {"daily_prices": 0, "index_daily_closes": 0}
    with sqlite3.connect(db) as connection:
        return {
            "daily_prices": connection.execute(
                "SELECT COUNT(*) FROM daily_prices"
            ).fetchone()[0],
            "index_daily_closes": connection.execute(
                "SELECT COUNT(*) FROM index_daily_closes"
            ).fetchone()[0],
        }


def _live_storage_check(db: Path) -> dict[str, object]:
    evidence = {**_sqlite_evidence(db), "coverage": _coverage_evidence(db)}
    rows = [*evidence["coverage"]["equity"], *evidence["coverage"]["indexes"]]
    if not rows:
        status = "blocked"
        summary = "No live provider acquisition produced stored observations."
    else:
        status = (
            "pass"
            if evidence["integrity_check"] == "ok"
            and evidence["duplicate_price_keys"] == 0
            and evidence["duplicate_index_keys"] == 0
            and all(
                row["sessions"] > 0 and row["first_session"] <= row["last_session"]
                for row in rows
            )
            else "fail"
        )
        summary = "Live SQLite integrity, duplicate-key, and coverage checks completed."
    return {"status": status, "summary": summary, "evidence": evidence}


def _run_synthetic(db: Path) -> list[dict[str, object]]:
    from finintel.market_data.daily_acquisition_store import (
        cancel_job,
        create_job,
        finish_job,
        get_job,
        get_settings,
        record_item,
        retry_failed_job,
        set_item_running,
        set_job_pause,
    )
    from finintel.market_data.index_history import read_index_closes, store_index_closes
    from finintel.storage.db import import_snapshot

    checks: list[dict[str, object]] = []
    settings = get_settings(db=db)
    checks.append(
        _check(
            "scheduler-disabled-by-default",
            "pass" if settings["enabled"] is False else "fail",
            "Automatic daily acquisition remains disabled in a new acceptance database.",
            {"enabled": settings["enabled"]},
        )
    )

    batch = _synthetic_batch()
    with tempfile.TemporaryDirectory(
        prefix="finintel-acceptance-snapshot-"
    ) as directory:
        snapshot = Path(directory) / "AAPL.json"
        snapshot.write_text(batch.model_dump_json() + "\n", encoding="utf-8")
        first = import_snapshot(snapshot, db=db)
        second = import_snapshot(snapshot, db=db)
    checks.append(
        _check(
            "synthetic-equity-idempotence",
            "pass"
            if first["inserted"] == 3
            and second["inserted"] == 0
            and second["unchanged"] == 3
            else "fail",
            "Repeated validated equity imports preserve one row per provider session.",
            {
                "provider": first["provider"],
                "first_session": batch.first_session.isoformat(),
                "last_successful_session": batch.last_session.isoformat(),
                "source_url": batch.source_url,
                "first_inserted": first["inserted"],
                "second_inserted": second["inserted"],
                "second_unchanged": second["unchanged"],
            },
        )
    )

    observations = [
        ("2026-10-27", "100.5"),
        ("2026-10-28", "101.5"),
        ("2026-10-29", "102.5"),
    ]
    first_index = store_index_closes(DEFAULT_INDEX, observations, db=db)
    second_index = store_index_closes(DEFAULT_INDEX, observations, db=db)
    stored_index = read_index_closes(DEFAULT_INDEX, db=db)
    checks.append(
        _check(
            "synthetic-index-idempotence",
            "pass"
            if first_index["inserted"] == 3
            and second_index["inserted"] == 0
            and second_index["unchanged"] == 3
            and len(stored_index["observations"]) == 3
            else "fail",
            "Repeated close-only index imports preserve FRED provenance and session keys.",
            {
                "provider": first_index["provider"],
                "first_inserted": first_index["inserted"],
                "second_inserted": second_index["inserted"],
                "source_url": first_index["source_url"],
            },
        )
    )

    lifecycle = create_job(
        kind="manual",
        provider="alpha_vantage",
        secondary_provider=None,
        scheduled_for="2026-10-30T12:00:00+00:00",
        schedule_date=None,
        equity_symbols=[DEFAULT_SYMBOL],
        index_codes=[],
        db=db,
    )
    lifecycle_id = str(lifecycle["id"])
    paused = set_job_pause(lifecycle_id, True, db=db)
    resumed = set_job_pause(lifecycle_id, False, db=db)
    cancelled = cancel_job(lifecycle_id, db=db)

    retry_source = create_job(
        kind="manual",
        provider="alpha_vantage",
        secondary_provider=None,
        scheduled_for="2026-10-30T12:01:00+00:00",
        schedule_date=None,
        equity_symbols=[DEFAULT_SYMBOL],
        index_codes=[],
        db=db,
    )
    retry_id = str(retry_source["id"])
    set_item_running(retry_id, "equity", DEFAULT_SYMBOL, db=db)
    record_item(
        retry_id,
        "equity",
        DEFAULT_SYMBOL,
        status="failed",
        provider="alpha_vantage",
        error_type="ProviderUnavailable",
        error_message="synthetic provider outage",
        db=db,
    )
    finish_job(retry_id, status="failed", db=db)
    retry = retry_failed_job(retry_id, db=db)
    checks.append(
        _check(
            "scheduler-controls-and-retry",
            "pass"
            if paused
            and paused["status"] == "paused"
            and resumed
            and resumed["status"] == "running"
            and cancelled
            and cancelled["status"] == "cancelled"
            and retry
            and get_job(str(retry["id"]), db=db)["status"] == "scheduled"
            else "fail",
            "Pause, resume, cancel, failure persistence, and retry-failed survive a new database run.",
            {"retry_job_id": retry["id"] if retry else None},
        )
    )

    evidence = _sqlite_evidence(db)
    checks.append(
        _check(
            "sqlite-integrity-and-unique-sessions",
            "pass"
            if evidence["integrity_check"] == "ok"
            and evidence["duplicate_price_keys"] == 0
            and evidence["duplicate_index_keys"] == 0
            else "fail",
            "SQLite integrity and provider/session uniqueness checks pass.",
            evidence,
        )
    )
    # Reopen through the public store API to exercise restart-safe persistence.
    persisted = get_settings(db=db)
    checks.append(
        _check(
            "restart-persistence",
            "pass"
            if persisted["enabled"] is False and get_job(lifecycle_id, db=db)
            else "fail",
            "Settings and job history remain readable after independent SQLite connections.",
            {"job_id": lifecycle_id, "enabled": persisted["enabled"]},
        )
    )
    return checks


def _configured(provider: str) -> bool:
    from finintel.market_data.alpaca import alpaca_configured
    from finintel.market_data.provider import key_configured
    from finintel.market_data.tiingo import tiingo_configured

    return {
        "alpha_vantage": key_configured,
        "alpaca_sip": alpaca_configured,
        "tiingo": tiingo_configured,
    }[provider]()


def _live_equity(db: Path, provider: str, symbols: list[str]) -> dict[str, object]:
    from finintel.market_data.acquisition import acquire_and_store
    from finintel.market_data.daily_acquisition_store import provider_usage_status
    from finintel.market_data.provider import (
        ProviderAccessDenied,
        ProviderBudgetExhausted,
        ProviderError,
        ProviderRateLimited,
        ProviderUnavailable,
    )
    from finintel.storage.db import StorageError, read_daily

    if not _configured(provider):
        return {
            "status": "blocked",
            "summary": "Provider credentials are not configured; no request was attempted.",
            "evidence": {"provider": provider, "symbols": symbols},
        }
    results: list[dict[str, object]] = []
    try:
        for symbol in symbols:
            first = acquire_and_store(
                symbol, provider=provider, outputsize="compact", period="10y"
            )
            second = acquire_and_store(
                symbol, provider=provider, outputsize="compact", period="10y"
            )
            series = read_daily(symbol, provider=provider, db=db, limit=4000)
            if series is None:
                raise RuntimeError("Stored equity series could not be read back")
            results.append(
                {
                    "symbol": symbol,
                    "provider": series["provider"],
                    "first_session": series["observations"][0]["session_date"],
                    "last_session": series["observations"][-1]["session_date"],
                    "source_url": series["source_url"],
                    "first_inserted": first["inserted"],
                    "second_inserted": second["inserted"],
                    "second_unchanged": second["unchanged"],
                    "requests_used": provider_usage_status(db=db)[provider][
                        "requests_used"
                    ],
                }
            )
        passed = all(item["second_inserted"] == 0 for item in results)
        return {
            "status": "pass" if passed else "fail",
            "summary": "Live equity acquisition and idempotent re-import completed.",
            "evidence": {"provider": provider, "symbols": results},
        }
    except (ProviderError, StorageError, OSError, ValueError, RuntimeError) as exc:
        blocked = isinstance(
            exc,
            (
                ProviderAccessDenied,
                ProviderBudgetExhausted,
                ProviderRateLimited,
                ProviderUnavailable,
            ),
        )
        return {
            "status": "blocked" if blocked else "fail",
            "summary": "Live equity acquisition did not complete.",
            "evidence": {"provider": provider, "error": _redact_message(exc)},
        }


def _live_index(db: Path, codes: list[str]) -> dict[str, object]:
    from finintel.market_data.daily_acquisition_store import provider_usage_status
    from finintel.market_data.index_history import (
        IndexHistoryError,
        acquire_index_closes,
        read_index_closes,
    )
    from finintel.market_data.provider import (
        ProviderAccessDenied,
        ProviderBudgetExhausted,
        ProviderRateLimited,
        ProviderUnavailable,
    )
    from finintel.storage.db import StorageError

    results: list[dict[str, object]] = []
    try:
        for code in codes:
            first = acquire_index_closes(code, db=db)
            second = acquire_index_closes(code, db=db)
            history = read_index_closes(code, db=db)
            if not history["observations"]:
                raise RuntimeError("Stored index history could not be read back")
            usage = provider_usage_status(db=db)["fred"]
            results.append(
                {
                    "code": code,
                    "provider": history["provider"],
                    "first_session": history["observations"][0]["session_date"],
                    "last_session": history["observations"][-1]["session_date"],
                    "source_url": history["source_url"],
                    "first_inserted": first["inserted"],
                    "second_inserted": second["inserted"],
                    "second_unchanged": second["unchanged"],
                    "requests_used": usage["requests_used"],
                    "daily_request_budget": usage["daily_request_budget"],
                    "quota_state": usage["quota_state"],
                }
            )
        passed = all(item["second_inserted"] == 0 for item in results)
        return {
            "status": "pass" if passed else "fail",
            "summary": "Live FRED acquisition and idempotent close-only re-import completed.",
            "evidence": {"provider": "fred", "indexes": results},
        }
    except (
        IndexHistoryError,
        ProviderAccessDenied,
        ProviderRateLimited,
        ProviderUnavailable,
        StorageError,
        OSError,
        RuntimeError,
    ) as exc:
        blocked = isinstance(
            exc,
            (
                ProviderAccessDenied,
                ProviderBudgetExhausted,
                ProviderRateLimited,
                ProviderUnavailable,
            ),
        )
        return {
            "status": "blocked" if blocked else "fail",
            "summary": "Live FRED acquisition did not complete.",
            "evidence": {"provider": "fred", "error": _redact_message(exc)},
        }


def _report(
    mode: str,
    synthetic_db: Path,
    live_db: Path | None,
    synthetic_checks: list[dict[str, object]],
    live_checks: list[dict[str, object]],
    live_results: dict[str, dict[str, object]],
    started_at: datetime,
    desktop_results: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    synthetic_verdict = _checks_verdict(synthetic_checks)
    matrix = _acceptance_matrix(live_results, desktop_results)
    production_verdict = (
        "not_run" if mode != "live" else _checks_verdict(live_checks + matrix)
    )
    verdict = _overall_verdict(mode, synthetic_verdict, production_verdict)
    return {
        "report_version": REPORT_VERSION,
        "run": {
            "mode": mode,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "synthetic_database": str(synthetic_db),
            "live_database": str(live_db) if live_db else None,
            "automatic_daily_acquisition_enabled": False,
            "credentials_logged": False,
        },
        "verdict": verdict,
        "synthetic_acceptance": {
            "verdict": synthetic_verdict,
            "checks": synthetic_checks,
        },
        "production_acceptance": {
            "verdict": production_verdict,
            "checks": live_checks,
            "matrix": matrix,
        },
        "checks": synthetic_checks + live_checks,
        "acceptance_matrix": matrix,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("synthetic", "live"), default="synthetic")
    parser.add_argument(
        "--db-path",
        type=Path,
        help="New disposable SQLite path for this mode; it must not exist",
    )
    parser.add_argument(
        "--synthetic-db-path",
        type=Path,
        help="New disposable SQLite path for synthetic checks; it must not exist",
    )
    parser.add_argument(
        "--live-db-path",
        type=Path,
        help="New disposable SQLite path for live checks; it must not exist",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/daily-acquisition-acceptance.json")
    )
    parser.add_argument("--provider", action="append", choices=EQUITY_PROVIDERS)
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--index", dest="indexes", action="append", choices=INDEX_CODES)
    parser.add_argument(
        "--desktop-evidence",
        type=Path,
        help=(
            "JSON mapping of desktop platforms to manual evidence; valid statuses "
            "are pass, fail, blocked, and not_run"
        ),
    )
    parser.add_argument(
        "--record-desktop-evidence",
        type=Path,
        metavar="REPORT",
        help="Merge --desktop-evidence into an existing live acceptance report",
    )
    args = parser.parse_args(argv)
    if args.record_desktop_evidence:
        if not args.desktop_evidence:
            parser.error("--record-desktop-evidence requires --desktop-evidence")
        if any(
            (
                args.db_path,
                args.synthetic_db_path,
                args.live_db_path,
                args.provider,
                args.symbol,
                args.indexes,
            )
        ):
            parser.error(
                "--record-desktop-evidence cannot be combined with acquisition options"
            )
        try:
            report_path = _validate_output_path(
                args.record_desktop_evidence, _known_database_paths()
            )
            desktop_results = _load_desktop_evidence(args.desktop_evidence)
            with report_path.open(encoding="utf-8") as stream:
                report = json.load(stream)
            if not isinstance(report, dict):
                raise TypeError("Acceptance report must be a JSON object")
            _apply_desktop_evidence(report, desktop_results)
            _atomic_write_report(report_path, report)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
            return 1
        print(f"Acceptance report: {report_path}")
        print("Verdict:", str(report["verdict"]).upper())
        return _report_exit_code(report)
    if args.desktop_evidence and args.mode != "live":
        parser.error("--desktop-evidence requires --mode live")
    if args.db_path and (args.synthetic_db_path or args.live_db_path):
        parser.error(
            "--db-path cannot be combined with --synthetic-db-path or --live-db-path"
        )
    if args.mode == "live" and not (args.provider or args.indexes):
        args.provider = list(EQUITY_PROVIDERS)
        args.indexes = [DEFAULT_INDEX]
    synthetic_path = args.synthetic_db_path
    live_path = args.live_db_path
    if args.db_path:
        if args.mode == "live":
            live_path = args.db_path
        else:
            synthetic_path = args.db_path
    output = args.output
    try:
        output = _validate_output_path(
            output,
            _known_database_paths()
            | {
                path.expanduser().resolve()
                for path in (synthetic_path, live_path)
                if path is not None
            },
        )
    except (OSError, ValueError) as exc:
        print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
        return 1
    try:
        desktop_results = (
            _load_desktop_evidence(args.desktop_evidence)
            if args.desktop_evidence
            else {}
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
        return 1
    started_at = datetime.now(UTC)

    try:
        with _disposable_database(synthetic_path) as synthetic_db:
            synthetic_checks = _run_synthetic(synthetic_db)
            if args.mode != "live":
                report = _report(
                    args.mode,
                    synthetic_db,
                    None,
                    synthetic_checks,
                    [],
                    {},
                    started_at,
                )
            else:
                with _disposable_database(live_path) as live_db:
                    live_checks: list[dict[str, object]] = []
                    initial_counts = _market_data_counts(live_db)
                    live_checks.append(
                        _check(
                            "live-database-start-empty",
                            "pass"
                            if initial_counts["daily_prices"] == 0
                            and initial_counts["index_daily_closes"] == 0
                            else "fail",
                            "Live acceptance starts without synthetic market observations.",
                            initial_counts,
                        )
                    )
                    live_results: dict[str, dict[str, object]] = {}
                    for provider in args.provider or []:
                        result = _live_equity(
                            live_db, provider, args.symbol or [DEFAULT_SYMBOL]
                        )
                        live_results[provider] = result
                        live_checks.append(
                            _check(
                                "live-equity-" + provider,
                                result["status"],
                                result["summary"],
                                result["evidence"],
                            )
                        )
                    if args.indexes:
                        result = _live_index(live_db, args.indexes)
                        live_results["fred"] = result
                        live_checks.append(
                            _check(
                                "live-index-fred",
                                result["status"],
                                result["summary"],
                                result["evidence"],
                            )
                        )
                    storage = _live_storage_check(live_db)
                    live_checks.append(
                        _check(
                            "live-sqlite-integrity-coverage",
                            storage["status"],
                            storage["summary"],
                            storage["evidence"],
                        )
                    )
                    report = _report(
                        args.mode,
                        synthetic_db,
                        live_db,
                        synthetic_checks,
                        live_checks,
                        live_results,
                        started_at,
                        desktop_results,
                    )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
        return 1

    try:
        _atomic_write_report(output, report)
    except OSError as exc:
        print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
        return 1
    print(f"Acceptance report: {output}")
    print("Verdict:", str(report["verdict"]).upper())
    for check in report["checks"]:
        print(f"{str(check['status']).upper():8} {check['id']}: {check['summary']}")
    return _report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
