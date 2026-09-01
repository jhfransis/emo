"""트레일링 워커 백그라운드 기동."""

from __future__ import annotations

import atexit
import fcntl
import os
import subprocess
import sys
from pathlib import Path

from kis_client import get_active_profile

from .trailing_db import DB_DIR

PID_PATH = DB_DIR / "trailing_worker.pid"
LOCK_PATH = DB_DIR / "trailing_worker.lock"
LOG_PATH = DB_DIR / "trailing_worker.log"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKER_SCRIPT = Path(__file__).resolve().parent / "trailing_run.py"
_lock_fd = None


def read_worker_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (TypeError, ValueError):
        return None


def clear_worker_pid() -> None:
    PID_PATH.unlink(missing_ok=True)


def write_worker_pid(pid: int | None = None) -> None:
    pid = pid or os.getpid()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid), encoding="utf-8")


def _pid_is_trailing_worker(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    return "trailing_run.py" in cmd


def worker_process_alive() -> bool:
    pid = read_worker_pid()
    if pid is None:
        return False
    if not _pid_is_trailing_worker(pid):
        clear_worker_pid()
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        clear_worker_pid()
        return False


def acquire_worker_lock() -> bool:
    """프로세스 생애 동안 exclusive flock. 이미 떠 있으면 False."""
    global _lock_fd
    if _lock_fd is not None:
        return True
    DB_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_fd = handle
    return True


def release_worker_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        _lock_fd.close()
    except OSError:
        pass
    _lock_fd = None


def register_worker_pid_lifecycle() -> None:
    write_worker_pid()
    atexit.register(clear_worker_pid)
    atexit.register(release_worker_lock)


def start_worker(profile: str | None = None, *, dry_run: bool = False) -> bool:
    if worker_process_alive():
        return False

    profile = profile or get_active_profile()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_PATH, "a", encoding="utf-8")

    cmd = [sys.executable, str(_WORKER_SCRIPT), "--immediate", "--profile", profile]
    if dry_run:
        cmd.append("--dry-run")

    env = os.environ.copy()
    src_dir = str(_PROJECT_ROOT / "src")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
    )

    subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return True
