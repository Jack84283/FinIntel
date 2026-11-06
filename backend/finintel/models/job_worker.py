"""Issue #58: isolated, local-only execution of source-pinned model jobs.

The FastAPI process only supervises a *separate* child process. No provider
requests, third-party model modules, trading, or unpinned retries are allowed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from finintel.market_data.analytics import AnalyticsError
from finintel.market_data.analytics_store import run_analytics
from finintel.models.engine import evaluate_builtin_indicator
from finintel.models.job_store import ModelJobLedger, ModelJobStoreError
from finintel.models.registry import MAX_MODEL_OBSERVATIONS
from finintel.storage.db import StorageError

POLL_SECONDS = 0.2
MAX_RUNTIME_SECONDS = 90
TERMINATE_GRACE_SECONDS = 2
# Additional worker memory guard: Linux address space, Windows job object,
# and a macOS RSS watchdog. The latter is polling rather than a hard kernel cap.
MAX_WORKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

# Do not inherit provider credentials, API keys, proxy endpoints or a Python import path.
CHILD_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "VIRTUAL_ENV",
        "FININTEL_DB_PATH",
        "FININTEL_MODEL_JOBS_DB_PATH",
        "LANG",
        "LC_ALL",
    }
)


def _disabled_network(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("Network access is unavailable to local model workers")


def _deny_outbound_network() -> None:
    # Block all outbound socket APIs used by Python HTTP/provider clients.
    socket.socket.connect = _disabled_network  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = _disabled_network  # type: ignore[method-assign,assignment]
    socket.socket.sendto = _disabled_network  # type: ignore[method-assign,assignment]
    socket.create_connection = _disabled_network  # type: ignore[assignment]


def _bound_child_memory() -> None:
    if sys.platform != "linux":
        return
    import resource  # Unix only; Linux address-space limit is a secondary guard.

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = MAX_WORKER_MEMORY_BYTES
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    if soft == resource.RLIM_INFINITY or soft > ceiling:
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))


def _limit_windows_worker(process: subprocess.Popen[bytes]) -> int | None:
    """Assign a child to a bounded Windows Job Object, fail closed on errors."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

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
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("Model-worker memory isolation unavailable")
    try:
        limits = ExtendedLimits()
        # Terminate remaining members if supervision exits unexpectedly.
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        limits.ProcessMemoryLimit = MAX_WORKER_MEMORY_BYTES
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise OSError("Model-worker memory isolation unavailable")
        if not kernel.AssignProcessToJobObject(handle, ctypes.c_void_p(process._handle)):
            raise OSError("Model-worker memory isolation unavailable")
        return int(handle)
    except Exception:
        kernel.CloseHandle(handle)
        raise


def _close_windows_job(handle: int | None) -> None:
    if handle is not None and os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        kernel.CloseHandle(ctypes.c_void_p(handle))


def _macos_rss_bytes(pid: int) -> int | None:
    """Read the actual resident KiB with the system ps utility, not virtual size."""
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        raw = output.stdout.strip()
        return int(raw) * 1024 if output.returncode == 0 and raw.isdigit() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def source_snapshot_for_job(
    *,
    code: str,
    start: date,
    end: date,
    quality_policy: str,
) -> str:
    """Read only local bounded observations; do not run the model on an HTTP thread."""
    response = run_analytics(
        kind="index",
        code=code,
        provider="fred",
        start=start,
        end=end,
        window=20,
        max_observations=MAX_MODEL_OBSERVATIONS,
        quality_policy=quality_policy,
    )
    identity = response["identity"]
    if (
        identity.get("instrument_kind") != "index"
        or identity.get("provider") != "fred"
        or identity.get("adjustment") != "price_index"
        or identity.get("return_semantics") != "price_index"
        or identity.get("frequency") != "daily"
        or identity.get("units") != "index_points"
    ):
        raise AnalyticsError("Unsupported local model source identity")
    return response["source"]["snapshot_sha256"]


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, StorageError):
        return "storage_unavailable"
    if isinstance(exc, AnalyticsError):
        message = str(exc).lower()
        if "snapshot changed" in message or "source revision" in message:
            return "source_changed"
        if "quality" in message or "unverified" in message:
            return "quality_blocked"
        if "2,000" in message or "exceeds" in message:
            return "resource_limit"
        return "invalid_input"
    return "worker_failure"


def run_child(job_id: str, attempt: int) -> int:
    """Executed only in a spawned worker process, never in the API process."""
    try:
        if str(uuid.UUID(job_id)) != job_id or not 1 <= attempt <= 3:
            return 2
    except (ValueError, AttributeError):
        return 2
    _watch_supervisor_parent()
    _deny_outbound_network()
    _bound_child_memory()
    ledger = ModelJobLedger()
    try:
        request = ledger.read_claim(job_id, attempt)
        if request is None:
            return 0  # Cancelled/stale; no result is allowed.
        actual_before = source_snapshot_for_job(
            code=request.code,
            start=request.start,
            end=request.end,
            quality_policy=request.quality_policy,
        )
        if actual_before != request.expected_snapshot_sha256:
            ledger.fail(job_id, attempt, "source_changed")
            return 2
        result = evaluate_builtin_indicator(
            model_id=request.model_id,
            model_version=request.model_version,
            code=request.code,
            start=request.start,
            end=request.end,
            parameters=request.parameters,
            quality_policy=request.quality_policy,
            expected_snapshot_sha256=request.expected_snapshot_sha256,
        )
        # Fail closed if the source was revised while this child was executing.
        actual_after = source_snapshot_for_job(
            code=request.code,
            start=request.start,
            end=request.end,
            quality_policy=request.quality_policy,
        )
        if actual_after != request.expected_snapshot_sha256:
            ledger.fail(job_id, attempt, "source_changed")
            return 2
        if not ledger.complete(job_id, attempt, result):
            ledger.finish_cancelled(job_id, attempt)
        return 0
    except (AnalyticsError, StorageError, ModelJobStoreError, ValueError) as exc:
        ledger.fail(job_id, attempt, _safe_error_code(exc))
        ledger.finish_cancelled(job_id, attempt)
        return 2
    except Exception:  # noqa: BLE001 - worker must not leak unexpected exception details
        # Never print private exception arguments or local file paths.
        ledger.fail(job_id, attempt, "worker_failure")
        ledger.finish_cancelled(job_id, attempt)
        return 3


def _parent_alive(parent_pid: int) -> bool:
    if os.name == "nt":
        # A Windows subprocess is not necessarily killed when its launcher dies.
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    # PyInstaller one-file executables place a bootloader between the sidecar
    # and Python worker. A direct PPID equality check would kill every packaged
    # worker even though its original supervisor is still running.
    try:
        os.kill(parent_pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _watch_supervisor_parent() -> None:
    """A watchdog prevents orphaned computation after an unexpected sidecar crash."""
    raw = os.environ.get("FININTEL_WORKER_PARENT_PID", "")
    if not raw.isascii() or not raw.isdigit():
        return
    parent_pid = int(raw)
    if parent_pid <= 1:
        return

    def watch() -> None:
        while _parent_alive(parent_pid):
            time.sleep(POLL_SECONDS)
        # Parent death: no private log, and new sidecar will reconcile this attempt.
        os._exit(4)

    threading.Thread(target=watch, name="finintel-model-parent-watch", daemon=True).start()


def _worker_command(job_id: str, attempt: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--model-job-worker", job_id, str(attempt)]
    return [sys.executable, "-m", "finintel.models.job_worker", "--child", job_id, str(attempt)]


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except (OSError, ProcessLookupError):
        pass


class ModelJobSupervisor:
    """Single owning supervisor; a cross-process file lock prevents double recovery."""

    def __init__(self, ledger: ModelJobLedger | None = None) -> None:
        self.ledger = ledger or ModelJobLedger()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._child: subprocess.Popen[bytes] | None = None
        self._lock_fd: int | None = None

    def _acquire_lock(self) -> bool:
        path = self.ledger.path
        if (
            path.is_symlink()
            or path.resolve()
            == Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3"))
            .expanduser()
            .resolve()
        ):
            raise ModelJobStoreError("Unsafe model-job supervisor location")
        lock_path = path.with_name(path.name + ".worker.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise ModelJobStoreError("Unsafe model-job supervisor lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _release_lock(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        # Start without opening the lock. An idle packaged sidecar must not leave
        # a Windows-held lock file behind when a launcher terminates abruptly.
        # _loop acquires ownership only once an actual ledger exists.
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="finintel-model-supervisor", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        child = self._child
        if child is not None:
            _stop_process(child)
        if self._thread is not None:
            self._thread.join(timeout=MAX_RUNTIME_SECONDS + 5)
            if self._thread.is_alive():
                return  # Never release the exclusive lock while this thread is still alive.
        self._thread = None
        self._release_lock()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if self._lock_fd is None:
                        if not self.ledger.path.is_file():
                            self._stop.wait(POLL_SECONDS)
                            continue
                        # Only one sidecar may reconcile or claim from this ledger.
                        if not self._acquire_lock():
                            self._stop.wait(POLL_SECONDS)
                            continue
                        self.ledger.reconcile_interrupted()
                    claim = self.ledger.claim_next()
                    if claim is not None:
                        self._run_claim(*claim)
                        continue
                    self._stop.wait(POLL_SECONDS)
                except (OSError, ModelJobStoreError):
                    # A temporary SQLite failure must not silently disable jobs.
                    # Release ownership so the next acquisition reconciles any
                    # interrupted claim before another worker can start.
                    # No paths, credentials or underlying errors are logged.
                    self._release_lock()
                    self._stop.wait(max(POLL_SECONDS, 1.0))
        finally:
            self._release_lock()

    def _run_claim(self, job_id: str, request: object, attempt: int) -> None:
        del request  # Private input stays in the ledger; child reloads it by claim ID.
        env = {key: value for key, value in os.environ.items() if key in CHILD_ENV_KEYS}
        # Explicit resolved locations are required for both sources in packaged and dev modes.
        from finintel.storage.db import database_path

        env["FININTEL_DB_PATH"] = str(database_path().resolve())
        env["FININTEL_MODEL_JOBS_DB_PATH"] = str(self.ledger.path.resolve())
        env["FININTEL_WORKER_PARENT_PID"] = str(os.getpid())
        process: subprocess.Popen[bytes] | None = None
        windows_job: int | None = None
        try:
            process = subprocess.Popen(
                _worker_command(job_id, attempt),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self._child = process
            windows_job = _limit_windows_worker(process)
            deadline = time.monotonic() + MAX_RUNTIME_SECONDS
            next_macos_check = 0.0
            while process.poll() is None:
                if sys.platform == "darwin" and time.monotonic() >= next_macos_check:
                    rss = _macos_rss_bytes(process.pid)
                    if rss is None or rss > MAX_WORKER_MEMORY_BYTES:
                        _stop_process(process)
                        self.ledger.fail(job_id, attempt, "resource_limit")
                        return
                    next_macos_check = time.monotonic() + 1.0
                if self._stop.is_set():
                    _stop_process(process)
                    self.ledger.fail(job_id, attempt, "interrupted")
                    return
                state = self.ledger.get(job_id)
                if state and state["cancel_requested"]:
                    _stop_process(process)
                    self.ledger.finish_cancelled(job_id, attempt)
                    return
                if time.monotonic() >= deadline:
                    _stop_process(process)
                    self.ledger.fail(job_id, attempt, "resource_limit")
                    self.ledger.finish_cancelled(job_id, attempt)
                    return
                self._stop.wait(POLL_SECONDS)
            # A child can exit normally after an API-driven cancel or stale generation.
            state = self.ledger.get(job_id)
            if state and state["state"] == "running":
                if state["cancel_requested"]:
                    self.ledger.finish_cancelled(job_id, attempt)
                elif self._stop.is_set():
                    self.ledger.fail(job_id, attempt, "interrupted")
                else:
                    self.ledger.fail(job_id, attempt, "worker_failure")
        except (OSError, ModelJobStoreError):
            self.ledger.fail(job_id, attempt, "worker_failure")
        finally:
            if process is not None:
                _stop_process(process)
            _close_windows_job(windows_job)
            self._child = None


_supervisor: ModelJobSupervisor | None = None
_supervisor_lock = threading.Lock()


def start_model_jobs() -> None:
    global _supervisor
    # Worker startup and execution require a managed local sidecar process.
    if len(os.environ.get("FININTEL_PROCESS_TOKEN", "")) < 32:
        return
    with _supervisor_lock:
        if _supervisor is None:
            candidate = ModelJobSupervisor()
            try:
                if candidate.start():
                    _supervisor = candidate
            except (OSError, ModelJobStoreError):
                # Model-job availability must not take down unrelated market services.
                return


def stop_model_jobs() -> None:
    global _supervisor
    with _supervisor_lock:
        if _supervisor is not None:
            _supervisor.stop()
            _supervisor = None


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "--child":
        return 2
    try:
        attempt = int(sys.argv[3])
    except ValueError:
        return 2
    return run_child(sys.argv[2], attempt)


if __name__ == "__main__":
    sys.exit(main())
