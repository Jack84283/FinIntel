"""Executable entry point for the packaged, local-only FinIntel Python sidecar."""

import os

import uvicorn

from finintel.main import app


def main() -> None:
    port = int(os.environ["FININTEL_BACKEND_PORT"])
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid desktop backend port")
    if len(os.environ.get("FININTEL_PROCESS_TOKEN", "")) < 32:
        raise ValueError("A private desktop process token is required")
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
