"""Executable entry point for the packaged, local-only FinIntel Python sidecar."""

import os
import sys
import threading
import time

import uvicorn


def _watch_frozen_launcher() -> None:
    """Exit the inner one-file process if its PyInstaller launcher disappears."""
    if not getattr(sys, "frozen", False):
        return
    launcher_pid = os.getppid()
    if launcher_pid <= 1:
        raise RuntimeError("Packaged launcher is unavailable")

    def watch() -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, launcher_pid)
            if not handle:
                os._exit(4)
            try:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            finally:
                kernel.CloseHandle(handle)
            os._exit(4)

        while os.getppid() == launcher_pid:
            time.sleep(0.2)
        os._exit(4)

    threading.Thread(
        target=watch, name="finintel-frozen-launcher-watch", daemon=True
    ).start()


def main() -> int:
    _watch_frozen_launcher()
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
