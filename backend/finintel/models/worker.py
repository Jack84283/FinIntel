"""Phase 2B1: single-owner supervisor and separate local model-computation process.

Only the compiled-in SMA evaluator can run. This module never imports a provider,
downloads data, accepts a command/path from an HTTP request, or writes price data.
The supervisor runs on its own thread; model calculations run in a child process.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_store import run_analytics
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.job_store import (
    MAX_RESULT_BYTES,
    ModelJobLedger,
    ModelJobRequest,
    ModelJobStoreError,
)
from finintel.storage.db import StorageError, database_path

POLL_SECONDS = 0.2
WALL_TIMEOUT_SECONDS = 60
CANCEL_GRACE_SECONDS = 2
MEMORY_LIMIT_BYTES = 2 * 1024**3
MAX_ENVELOPE_BYTES = 8192


def _restrict_resources() -> None:
    """Apply hard process limits where available; fail closed if installation fails."""
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (50, 55))
    elif os.name == "nt":
        # A Windows Job Object is stronger than merely lowering the working set.
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError("Model process memory guard unavailable")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        limits.ProcessMemoryLimit = MEMORY_LIMIT_BYTES
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError("Model process memory guard unavailable")
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise OSError("Model process memory guard unavailable")
        # Retain the Job Object for the entire child lifetime.
        _windows_job_handles.append(job)


_windows_job_handles: list[object] = []


def _disable_network() -> None:
    """Defense in depth: no socket connects/sends from built-in model execution."""

    def denied(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Network access is disabled for local research jobs")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    socket.create_connection = denied


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, AnalyticsError):
        description = str(exc).lower()  # inspected locally; NEVER returned or logged
        if "snapshot" in description:
            return "source_changed"
        if "quality" in description or "scan" in description:
            return "quality_blocked"
        if "2,000" in description or "observation limit" in description:
            return "resource_limit"
        return "invalid_input"
    if isinstance(exc, (StorageError, ModelJobStoreError, OSError, sqlite3.Error)):
        return "storage_unavailable"
    if isinstance(exc, MemoryError):
        return "resource_limit"
    return "worker_failure"


def _active(ledger: ModelJobLedger, identifier: str, attempt: int) -> str:
    record = ledger.get(identifier)
    if record is None or record["attempts"] != attempt or record["state"] != "running":
        return "stale"
    return "cancelled" if record["cancel_requested"] else "running"


def _evaluate_claim(identifier: str, attempt: int, request: ModelJobRequest) -> dict:
    ledger = ModelJobLedger()
    if _active(ledger, identifier, attempt) != "running":
        return {"kind": "cancelled"}
    # The engine checks the pinned snapshot immediately before model calculation.
    result = evaluate_builtin_indicator(
        model_id=request.model_id,
        model_version=request.model_version,
        code=request.code,
        start=request.start,
        end=request.end,
        parameters=request.parameters,
        quality_policy=request.quality_policy,
        expected_snapshot_sha256=request.expected_snapshot_sha256,
        db=database_path(),
    )
    if _active(ledger, identifier, attempt) != "running":
        return {"kind": "cancelled"}
    # Re-read from the read-only SQLite source after computation, before returning
    # anything for durable storage. A changed source always fails closed.
    latest = run_analytics(
        kind="index",
        code=request.code,
        provider="fred",
        start=request.start,
        end=request.end,
        window=20,
        max_observations=2000,
        quality_policy=request.quality_policy,
        db=database_path(),
    )
    if latest["source"]["snapshot_sha256"] != request.expected_snapshot_sha256:
        return {"kind": "error", "code": "source_changed"}
    if _active(ledger, identifier, attempt) != "running":
        return {"kind": "cancelled"}
    return {"kind": "completed", "result": result}


def child_main() -> None:
    """Fixed child protocol. Never echo requests, paths or exception messages."""
    try:
        _restrict_resources()
        _disable_network()
        raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
        if not raw or len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("Invalid bounded child envelope")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"id", "attempt", "request"}:
            raise ValueError("Invalid child envelope")
        identifier = envelope["id"]
        attempt = envelope["attempt"]
        if not isinstance(identifier, str) or len(identifier) != 36:
            raise ValueError("Invalid job identifier")
        ModelJobLedger._validate_attempt(attempt)
        request = ModelJobRequest.model_validate(envelope["request"])
        response = _evaluate_claim(identifier, attempt, request)
        if len(json.dumps(response, separators=(",", ":")).encode()) > MAX_RESULT_BYTES:
            response = {"kind": "error", "code": "resource_limit"}
    except (
        AnalyticsError,
        StorageError,
        ModelJobStoreError,
        OSError,
        sqlite3.Error,
        MemoryError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
    ) as exc:
        response = {"kind": "error", "code": _safe_error(exc)}
    sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=True))
    sys.stdout.flush()


@contextlib.contextmanager
def _exclusive_owner(path: Path):
    """One supervisor per job ledger; do not reconcile another live owner."""
    lock_path = path.with_name(path.name + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ModelJobStoreError("Unsafe model worker lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _child_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--model-worker"]
    return [sys.executable, "-m", "finintel.models.worker"]


def _child_env(ledger: ModelJobLedger) -> dict[str, str]:
    # Avoid passing provider tokens, process credentials or arbitrary app secrets.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "TMP", "TEMP", "TMPDIR", "PYTHONPATH")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result["FININTEL_DB_PATH"] = str(database_path())
    result["FININTEL_MODEL_JOBS_DB_PATH"] = str(ledger.path)
    result["PYTHONIOENCODING"] = "utf-8"
    return result


class ModelJobSupervisor:
    """Bounded, single-concurrency queue consumer with killable subprocesses."""

    def __init__(self, ledger: ModelJobLedger | None = None):
        self.ledger = ledger or ModelJobLedger()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="finintel-model-supervisor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            self._terminate(self._process)
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=8)

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def _run(self) -> None:
        try:
            # An idle managed backend must not create a job database or lock.
            # The first explicit enqueue creates the ledger; poll until then.
            while not self._stop.is_set() and not self.ledger.path.is_file():
                self._stop.wait(POLL_SECONDS)
            if self._stop.is_set():
                return
            with _exclusive_owner(self.ledger.path) as owned:
                if not owned:
                    return
                self.ledger.reconcile_interrupted()
                while not self._stop.is_set():
                    claim = self.ledger.claim_next()
                    if claim is None:
                        self._stop.wait(POLL_SECONDS)
                    else:
                        self._execute(*claim)
        except (ModelJobStoreError, OSError, sqlite3.Error):
            # No exception or user-specific path reaches the API or frontend.
            return

    @staticmethod
    def _source_is_current(request: ModelJobRequest) -> bool:
        """Final read-only source pin check immediately before ledger.complete."""
        current = run_analytics(
            kind="index",
            code=request.code,
            provider="fred",
            start=request.start,
            end=request.end,
            window=20,
            max_observations=2000,
            quality_policy=request.quality_policy,
            db=database_path(),
        )
        return current["source"]["snapshot_sha256"] == request.expected_snapshot_sha256

    def _execute(self, identifier: str, request: ModelJobRequest, attempt: int) -> None:
        process = None
        reason = "worker_failure"
        try:
            if self._stop.is_set():
                self.ledger.fail(identifier, attempt, "interrupted")
                return
            options = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "env": _child_env(self.ledger),
                "close_fds": True,
            }
            if os.name == "posix":
                options["start_new_session"] = True
            else:
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            with self._process_lock:
                if self._stop.is_set():
                    self.ledger.fail(identifier, attempt, "interrupted")
                    return
                process = subprocess.Popen(_child_command(), **options)
                self._process = process
            envelope = json.dumps(
                {"id": identifier, "attempt": attempt, "request": request.model_dump(mode="json")}
            ).encode()
            started = time.monotonic()
            cancel_at = None
            first = True
            while True:
                status = _active(self.ledger, identifier, attempt)
                if status != "running":
                    if status == "stale":
                        return
                    if cancel_at is None:
                        cancel_at = time.monotonic()
                    if time.monotonic() - cancel_at >= CANCEL_GRACE_SECONDS:
                        self._terminate(process)
                        break
                if self._stop.is_set():
                    reason = "interrupted"
                    self._terminate(process)
                    break
                if time.monotonic() - started >= WALL_TIMEOUT_SECONDS:
                    reason = "resource_limit"
                    self._terminate(process)
                    break
                try:
                    output, _ = process.communicate(
                        input=envelope if first else None, timeout=POLL_SECONDS
                    )
                    if _active(self.ledger, identifier, attempt) == "cancelled":
                        break
                    if len(output) > MAX_RESULT_BYTES:
                        reason = "resource_limit"
                        break
                    response = json.loads(output)
                    if not isinstance(response, dict):
                        break
                    if response.get("kind") == "completed":
                        try:
                            if not self._source_is_current(request):
                                reason = "source_changed"
                                break
                        except (AnalyticsError, StorageError) as exc:
                            reason = _safe_error(exc)
                            break
                        if self.ledger.complete(identifier, attempt, response["result"]):
                            return
                    elif response.get("kind") == "error" and response.get("code") in {
                        "source_changed",
                        "quality_blocked",
                        "invalid_input",
                        "resource_limit",
                        "storage_unavailable",
                        "worker_failure",
                    }:
                        reason = response["code"]
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    continue
            if (
                self.ledger.get(identifier)
                and _active(self.ledger, identifier, attempt) == "cancelled"
            ):
                self.ledger.finish_cancelled(identifier, attempt)
            else:
                self.ledger.fail(identifier, attempt, reason)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ModelJobStoreError):
            if _active(self.ledger, identifier, attempt) == "cancelled":
                self.ledger.finish_cancelled(identifier, attempt)
            else:
                self.ledger.fail(identifier, attempt, reason)
        finally:
            self._terminate(process)
            if process is not None:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.communicate(timeout=2)
            with self._process_lock:
                if self._process is process:
                    self._process = None


_supervisor: ModelJobSupervisor | None = None


def start_supervisor() -> None:
    global _supervisor
    # An unmanaged developer server must not silently become a model executor.
    if len(os.environ.get("FININTEL_PROCESS_TOKEN", "")) < 32 or _supervisor is not None:
        return
    _supervisor = ModelJobSupervisor()
    _supervisor.start()


def stop_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        _supervisor.stop()
        _supervisor = None


if __name__ == "__main__":
    child_main()
