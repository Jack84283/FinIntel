"""Executable entry point for the packaged, local-only FinIntel Python sidecar."""

import os
import sys

import uvicorn


def main() -> int:
    # Frozen one-file executables must re-enter this entry point for the worker.
    # Worker children do not start the HTTP server and inherit no provider secrets.
    if len(sys.argv) == 4 and sys.argv[1] == "--model-job-worker":
        try:
            attempt = int(sys.argv[3])
        except ValueError:
            return 2
        from finintel.models.job_worker import run_child

        return run_child(sys.argv[2], attempt)
    if len(sys.argv) != 1:
        raise ValueError("Unsupported desktop sidecar arguments")

    port = int(os.environ["FININTEL_BACKEND_PORT"])
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid desktop backend port")
    if len(os.environ.get("FININTEL_PROCESS_TOKEN", "")) < 32:
        raise ValueError("A private desktop process token is required")
    from finintel.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
