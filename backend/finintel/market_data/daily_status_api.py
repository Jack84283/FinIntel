"""Read-only monitoring endpoint for the local daily worker."""
from pathlib import Path

from fastapi import APIRouter

from finintel.market_data.daily_scheduler import status

router = APIRouter(prefix="/api/v1/daily-acquisition", tags=["daily-acquisition"])


@router.get("/status")
def daily_acquisition_status() -> dict:
    snapshot = status()
    # Return aggregate progress, not the potentially enormous per-symbol map.
    return {
        "status": snapshot.get("status", "never_run"),
        "provider": snapshot.get("provider"),
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "last_completed_date": snapshot.get("last_completed_date"),
        "completed": snapshot.get("completed", 0),
        "failed": snapshot.get("failed", 0),
        "total_equities": snapshot.get("total_equities", 0),
        "current_symbol": snapshot.get("current_symbol"),
        "indexes": snapshot.get("indexes", {}),
    }
