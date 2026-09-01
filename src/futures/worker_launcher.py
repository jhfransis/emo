"""트레일링 워커 백그라운드 기동."""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
from pathlib import Path

from kis_client import get_active_profile

from .trailing_db import DB_DIR

PID_PATH = DB_DIR / "trailing_worker.pid"
LOG_PATH = DB_DIR / "trailing_worker.log"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKER_SCRIPT = Path(__file__).resolve().parent / "trailing_run.py"


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


def worker_process_alive() -> bool:
    pid = read_worker_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        clear_worker_pid()
        return False


def register_worker_pid_lifecycle() -> None:
    write_worker_pid()
    atexit.register(clear_worker_pid)


def start_worker(profile: str | None = None, *, dry_run: bool = False) -> bool:
    """백그라운드 워커 프로세스를 시작한다. 이미 실행 중이면 False."""
    if worker_process_alive():
        return False

    profile = profile or get_active_profile()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_PATH, "a", encoding="utf-8")

    cmd = [sys.executable, str(_WORKER_SCRIPT), "--immediate"]
    if dry_run:
        cmd.append("--dry-run")

    env = os.environ.copy()
    src_dir = str(_PROJECT_ROOT / "src")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
    )

    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    write_worker_pid(proc.pid)
    return True
