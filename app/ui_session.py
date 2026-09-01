"""UI 로그인 세션 (서버측, F5 후 URL sid로 복원)."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

SESSION_DIR = Path(__file__).resolve().parent.parent / "db" / "ui_sessions"
SESSION_HOURS = 12


def _session_path(sid: str) -> Path:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return SESSION_DIR / f"{safe}.json"


def create_session(*, hours: float = SESSION_HOURS) -> str:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sid = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=hours)
    _session_path(sid).write_text(
        json.dumps({"expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8",
    )
    return sid


def validate_session(sid: str | None) -> datetime | None:
    if not sid:
        return None
    path = _session_path(sid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError, OSError):
        path.unlink(missing_ok=True)
        return None
    if datetime.now() >= expires_at:
        path.unlink(missing_ok=True)
        return None
    return expires_at


def delete_session(sid: str | None) -> None:
    if not sid:
        return
    _session_path(sid).unlink(missing_ok=True)
