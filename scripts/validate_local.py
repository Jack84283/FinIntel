"""Run local FinIntel acceptance against a running backend, without changing SQLite.

Usage: python scripts/validate_local.py --output data/acceptance-report.json
Optional provider lookup: add --corporate-symbol AAPL.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHECKS = ["coverage", "integrity", "corporate", "provider", "performance", "quality"]
BASE = "http://127.0.0.1:8765/api/v1/validation"


def request(endpoint: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(
        BASE + endpoint,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(req, timeout=90) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Backend HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError("Cannot reach the local backend at 127.0.0.1:8765") from exc


def start_or_join() -> str:
    """Start a scan, or join an already-running compatible scan on HTTP 409."""
    try:
        job = request("/run", {"checks": CHECKS})
    except RuntimeError as exc:
        if not str(exc).startswith("Backend HTTP 409:"):
            raise
        job = request("/status")
        if job.get("status") not in ("queued", "running"):
            raise RuntimeError("Validation conflict, but no active job is available.") from exc
        if set(job.get("checks", [])) != set(CHECKS):
            raise RuntimeError(
                "Another validation is running with different checks; cannot reuse its results."
            ) from exc
        print("Joining already-running six-check validation", flush=True)
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError("Validation backend did not return a job identifier.")
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/acceptance-report.json"))
    parser.add_argument("--corporate-symbol", help="Optional explicit Tiingo event check")
    parser.add_argument("--timeout", type=int, default=3600, help="Maximum scan seconds")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    print("Starting local validation of all six checks", flush=True)
    job_id = start_or_join()
    deadline = time.monotonic() + args.timeout
    while True:
        status = request("/status")
        if status.get("id") != job_id:
            raise RuntimeError("Validation job changed while waiting; refusing unrelated results.")
        if status["status"] in ("completed", "failed"):
            break
        if time.monotonic() >= deadline:
            print("Scan still running. No data was changed; inspect the UI for status.")
            return 2
        time.sleep(2)
    if status["status"] != "completed":
        print("Validation failed:", status.get("error", "Unknown failure"), file=sys.stderr)
        return 1
    result = status.get("results", {})
    reference_error = None
    if args.corporate_symbol:
        try:
            result["corporate_reference"] = request(
                "/corporate-reference", {"symbol": args.corporate_symbol}
            )
        except RuntimeError as exc:
            # Preserve completed offline findings even if optional network
            # verification is unavailable (missing key, provider outage, etc.).
            reference_error = str(exc)
            result["corporate_reference_error"] = reference_error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.output}")
    print("OHLCV issues:", result.get("integrity", {}).get("issues", {}))
    coverage = result.get("coverage", {})
    print("XNYS gap candidates:", coverage.get("xnys_internal_gap_candidates", "unavailable"))
    performance = result.get("performance", {})
    print("Performance targets met:", performance.get("all_acceptance_targets_met"))
    print("Provider pairs compared:", result.get("provider", {}).get("cross_provider_pairs_compared"))
    print("Quality findings:", result.get("quality", {}).get("finding_count"))
    if reference_error:
        print("Optional corporate lookup failed:", reference_error, file=sys.stderr)
        print("Core scan report was saved; corporate reference needs review.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
