"""CI smoke test of the packaged, authenticated local Python sidecar."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BINARIES = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"


def _target() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    return next(line.partition(": ")[2] for line in output.splitlines() if line.startswith("host: "))


def main() -> None:
    binary = BINARIES / ("finintel-backend-" + _target() + (".exe" if os.name == "nt" else ""))
    if not binary.is_file():
        raise FileNotFoundError(f"Missing native sidecar: {binary.name}")

    with tempfile.TemporaryDirectory(prefix="finintel-sidecar-smoke-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(32)
        env = os.environ.copy()
        env.update({
            "FININTEL_BACKEND_PORT": str(port),
            "FININTEL_PROCESS_TOKEN": token,
            "FININTEL_DB_PATH": str(Path(directory) / "market.sqlite3"),
        })
        for key in tuple(env):
            if key.startswith(("FININTEL_ALPHA_VANTAGE_API_KEY", "FININTEL_ALPACA_API_",
                               "FININTEL_TIINGO_API_KEY", "FININTEL_EQUIBLES_API_KEY")):
                env.pop(key)
        process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}/api/v1/health"
            deadline = time.monotonic() + 60
            health = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Packaged sidecar exited unexpectedly: {process.returncode}")
                try:
                    with urlopen(Request(url, headers={"x-finintel-process-token": token}),
                                 timeout=1) as response:
                        health = json.load(response)
                        break
                except (URLError, TimeoutError, ConnectionError):
                    time.sleep(0.4)
            if health is None or health.get("status") != "ok":
                raise RuntimeError("Packaged local backend failed readiness check")
            try:
                urlopen(url, timeout=2)
                raise AssertionError("Missing process token should be rejected")
            except HTTPError as exc:
                if exc.code != 403:
                    raise AssertionError("Unexpected unauthenticated response") from exc
            print("PASS: packaged sidecar startup, health, token rejection and shutdown")
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
