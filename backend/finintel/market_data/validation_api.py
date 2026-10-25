"""Local-only, single-flight read-only validation jobs for the desktop UI."""

import threading
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from finintel.market_data.corporate_reference import verify_events
from finintel.market_data.diagnostics import diagnostics
from finintel.market_data.performance import benchmark
from finintel.market_data.provider import ProviderError
from finintel.market_data.validation import validate
from finintel.storage.db import database_path

router = APIRouter(prefix="/api/v1/validation", tags=["data-validation"])
CHECKS = ("coverage", "integrity", "corporate", "provider", "performance")
_lock = threading.Lock()
_job: dict | None = None


class StartRequest(BaseModel):
    checks: list[str] = Field(min_length=1, max_length=5)


def _snapshot() -> dict:
    assert _job is not None
    return dict(_job)


def _run(job_id: str, checks: list[str]) -> None:
    try:
        results = {}
        if "integrity" in checks:
            with _lock:
                assert _job is not None
                _job["stage"] = "Scanning OHLCV integrity"
            results["integrity"] = validate(database_path())
        if any(check in checks for check in ("coverage", "corporate", "provider")):
            with _lock:
                assert _job is not None
                _job["stage"] = "Scanning historical coverage and comparisons"
            diagnostic = diagnostics(database_path())
            for check in checks:
                if check == "coverage":
                    results["coverage"] = {
                        key: diagnostic[key]
                        for key in (
                            "series_count",
                            "series_without_prices",
                            "coverage_examples",
                            "weekday_gap_candidates",
                            "series_with_weekday_gap_candidates",
                            "gap_examples",
                            "xnys_internal_gap_candidates",
                            "series_with_xnys_internal_gaps",
                            "xnys_gap_examples",
                            "reference_symbol",
                            "reference_provider",
                            "reference_session_gap_candidates",
                            "series_with_reference_session_gaps",
                            "reference_gap_examples",
                            "observed_span_groups",
                            "series_with_less_than_9_years_observed",
                        )
                    }
                elif check == "corporate":
                    results["corporate"] = {
                        "corporate_action_jump_candidate_count": diagnostic[
                            "corporate_action_jump_candidate_count"
                        ],
                        "corporate_action_jump_candidates": diagnostic[
                            "corporate_action_jump_candidates"
                        ],
                    }
                elif check == "provider":
                    results["provider"] = {
                        "cross_provider_close_divergences_over_1pct": diagnostic[
                            "cross_provider_close_divergences_over_1pct"
                        ],
                        "cross_provider_pairs_compared": diagnostic[
                            "cross_provider_pairs_compared"
                        ],
                        "cross_provider_comparisons": diagnostic["cross_provider_comparisons"],
                    }

            results["limitations"] = diagnostic["limitations"]
        if "performance" in checks:
            with _lock:
                assert _job is not None
                _job["stage"] = "Benchmarking indexed SQLite queries"
            results["performance"] = benchmark(database_path())
        with _lock:
            if _job and _job["id"] == job_id:
                _job.update(
                    status="completed",
                    stage="Complete",
                    results=results,
                    finished_at=datetime.now(UTC).isoformat(),
                )
    except Exception as exc:  # noqa: BLE001 - background worker must surface failures
        with _lock:
            if _job and _job["id"] == job_id:
                _job.update(
                    status="failed",
                    stage="Failed",
                    error=str(exc),
                    finished_at=datetime.now(UTC).isoformat(),
                )


@router.post("/run")
def start_validation(request: StartRequest) -> dict:
    if len(set(request.checks)) != len(request.checks) or any(
        check not in CHECKS for check in request.checks
    ):
        raise HTTPException(status_code=422, detail="Invalid validation checks.")
    if not database_path().is_file():
        raise HTTPException(status_code=503, detail="Local market database is unavailable.")
    global _job
    with _lock:
        if _job and _job["status"] in ("queued", "running"):
            raise HTTPException(status_code=409, detail="A validation job is already running.")
        job_id = uuid4().hex
        _job = {
            "id": job_id,
            "checks": request.checks,
            "status": "running",
            "stage": "Starting",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "results": None,
            "error": None,
        }
        snapshot = _snapshot()
    threading.Thread(target=_run, args=(job_id, request.checks), daemon=True).start()
    return snapshot


@router.get("/status")
def validation_status() -> dict:
    with _lock:
        return _snapshot() if _job else {"status": "idle"}


class CorporateReferenceRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)


@router.post("/corporate-reference")
def verify_corporate_reference(request: CorporateReferenceRequest) -> dict:
    """Explicit provider verification; not triggered by ordinary local scans."""
    try:
        return verify_events(database_path(), request.symbol)
    except (ProviderError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
