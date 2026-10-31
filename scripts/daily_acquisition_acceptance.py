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
    db: Path,
    checks: list[dict[str, object]],
    started_at: datetime,
) -> dict[str, object]:
    statuses = [check["status"] for check in checks]
    if "fail" in statuses:
        verdict = "fail"
    elif any(status in ("blocked", "not_run") for status in statuses):
        verdict = "review"
    else:
        verdict = "pass"
    return {
        "report_version": REPORT_VERSION,
        "run": {
            "mode": mode,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "database": "temporary disposable SQLite database"
            if db.parent.name.startswith("finintel-daily-acceptance-")
            else "explicit disposable SQLite path",
            "automatic_daily_acquisition_enabled": False,
            "credentials_logged": False,
        },
        "verdict": verdict,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("synthetic", "live"), default="synthetic")
    parser.add_argument(
        "--db-path", type=Path, help="New disposable SQLite path; it must not exist"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/daily-acquisition-acceptance.json")
    )
    parser.add_argument("--provider", action="append", choices=EQUITY_PROVIDERS)
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--index", dest="indexes", action="append", choices=INDEX_CODES)
    args = parser.parse_args(argv)
    if args.mode == "live" and not (args.provider or args.indexes):
        args.provider = list(EQUITY_PROVIDERS)
        args.indexes = [DEFAULT_INDEX]
    started_at = datetime.now(UTC)

    try:
        with _disposable_database(args.db_path) as db:
            checks = _run_synthetic(db)
            if args.mode == "live":
                for provider in args.provider or []:
                    result = _live_equity(db, provider, args.symbol or [DEFAULT_SYMBOL])
                    checks.append(
                        _check(
                            "live-equity-" + provider,
                            result["status"],
                            result["summary"],
                            result["evidence"],
                        )
                    )
                if args.indexes:
                    result = _live_index(db, args.indexes)
                    checks.append(
                        _check(
                            "live-index-fred",
                            result["status"],
                            result["summary"],
                            result["evidence"],
                        )
                    )
            report = _report(args.mode, db, checks, started_at)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Acceptance harness failed:", _redact_message(exc), file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Acceptance report: {args.output}")
    print("Verdict:", str(report["verdict"]).upper())
    for check in report["checks"]:
        print(f"{str(check['status']).upper():8} {check['id']}: {check['summary']}")
    return {"pass": 0, "review": 2, "fail": 1}[str(report["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
