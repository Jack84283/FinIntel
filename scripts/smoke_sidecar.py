"""CI smoke test of the packaged, authenticated sidecar and frozen model worker.

Only disposable SQLite observations are created. No provider downloads or
credentials are required; the real built executable must dispatch its child.
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BINARIES = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"


def _target() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    return next(line.partition(": ")[2] for line in output.splitlines() if line.startswith("host: "))


def _seed_disposable_market(db: Path) -> None:
    from finintel.storage.db import _write_connection

    conn = _write_connection(db)
    try:
        first = date(2026, 9, 1)
        with conn:
            conn.executemany(
                "INSERT INTO index_daily_closes "
                "(index_code,provider,session_date,close,retrieved_at,source_url) "
                "VALUES (?,'fred',?,?,'2026-10-25T00:00:00+00:00',?)",
                [
                    (
                        "SP500",
                        (first + timedelta(days=i)).isoformat(),
                        str(100 + i),
                        "https://fred.stlouisfed.org/series/SP500",
                    )
                    for i in range(35)
                ],
            )
    finally:
        conn.close()


def _get_json(url: str, token: str, *, timeout: float = 5) -> dict:
    with urlopen(
        Request(url, headers={"x-finintel-process-token": token}), timeout=timeout
    ) as response:
        return json.load(response)


def _verify_packaged_worker(base: str, token: str, db: Path) -> None:
    """Exercise HTTP enqueue -> SQLite ledger -> frozen child -> verified API result."""
    _seed_disposable_market(db)
    payload = {
        "model_id": "builtin.sma_crossover",
        "model_version": "1.0.0",
        "code": "SP500",
        "start": "2026-09-01",
        "end": "2026-10-05",
        "quality_policy": "allow_unverified",
        "parameters": {"fast_window": 5, "slow_window": 20},
    }
    request = Request(
        base + "/api/v1/models/jobs",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-finintel-process-token": token,
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        if response.status != 202:
            raise AssertionError("Packaged sidecar refused a valid synthetic model job")
        queued = json.load(response)
    if queued.get("state") != "queued" or queued.get("status") != "queued":
        raise AssertionError("Packaged sidecar returned unexpected queued job state")
    job_id = queued["id"]
    url = base + "/api/v1/models/jobs/" + job_id + "?include_result=true"
    deadline = time.monotonic() + 120
    result = None
    while time.monotonic() < deadline:
        try:
            result = _get_json(url, token, timeout=5)
        except (URLError, TimeoutError):
            time.sleep(0.5)
            continue
        if result.get("state") in ("failed", "cancelled"):
            raise AssertionError(
                "Packaged model worker did not complete: " + str(result.get("error_code"))
            )
        if result.get("state") == "completed":
            break
        time.sleep(0.25)
    if result is None or result.get("state") != "completed":
        raise AssertionError("Packaged model child did not finish within the smoke-test deadline")

    output = result.get("result") or {}
    points = output.get("points")
    lineage = output.get("lineage")
    if (
        result.get("status") != "completed"
        or result.get("attempts") != 1
        or not isinstance(points, list)
        or len(points) != 35
        or not isinstance(lineage, dict)
        or output.get("status") != "completed"
        or output.get("read_only") is not True
        or output.get("no_network") is not True
        or output.get("backtest_validated") is not False
        or lineage.get("source_snapshot_sha256") != result.get("source_snapshot_sha256")
    ):
        raise AssertionError("Packaged model worker returned invalid result metadata")
    canonical_points = json.dumps(
        points, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    canonical_lineage = json.dumps(
        lineage, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if (
        hashlib.sha256(canonical_points).hexdigest() != result.get("output_sha256")
        or result["output_sha256"] != output.get("output_sha256")
        or hashlib.sha256(canonical_lineage).hexdigest()
        != output.get("reproducibility_sha256")
    ):
        raise AssertionError("Packaged worker output hashes did not match observations")
    print("PASS: packaged frozen worker executed a source-pinned synthetic model job")


def main() -> None:
    binary = BINARIES / ("finintel-backend-" + _target() + (".exe" if os.name == "nt" else ""))
    if not binary.is_file():
        raise FileNotFoundError(f"Missing native sidecar: {binary.name}")

    with tempfile.TemporaryDirectory(prefix="finintel-sidecar-smoke-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(32)
        market_db = Path(directory) / "market.sqlite3"
        env = os.environ.copy()
        env.update(
            {
                "FININTEL_BACKEND_PORT": str(port),
                "FININTEL_PROCESS_TOKEN": token,
                "FININTEL_DB_PATH": str(market_db),
                "FININTEL_MODEL_JOBS_DB_PATH": str(Path(directory) / "finintel-model-jobs.sqlite3"),
            }
        )
        for key in tuple(env):
            if key.startswith(
                (
                    "FININTEL_ALPHA_VANTAGE_API_KEY",
                    "FININTEL_ALPACA_API_",
                    "FININTEL_TIINGO_API_KEY",
                    "FININTEL_EQUIBLES_API_KEY",
                )
            ):
                env.pop(key)
        process = subprocess.Popen(
            [str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            base = f"http://127.0.0.1:{port}"
            health_url = base + "/api/v1/health"
            deadline = time.monotonic() + 60
            health = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Packaged sidecar exited unexpectedly")
                try:
                    health = _get_json(health_url, token, timeout=1)
                    break
                except (URLError, TimeoutError, ConnectionError):
                    time.sleep(0.4)
            if health is None or health.get("status") != "ok":
                raise RuntimeError("Packaged local backend failed readiness check")

            lock = Path(directory) / "finintel-model-jobs.sqlite3.worker.lock"
            if lock.exists():
                raise AssertionError("Idle sidecar unexpectedly opened model-worker lock")
            try:
                urlopen(health_url, timeout=2)
                raise AssertionError("Missing process token should be rejected")
            except HTTPError as exc:
                if exc.code != 403:
                    raise AssertionError("Unexpected unauthenticated response") from exc

            _verify_packaged_worker(base, token, market_db)
            print("PASS: packaged sidecar startup, token rejection and shutdown")
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
