"""워커 시각 정렬. 별도 스케줄러 프로세스 없이 한 루프에서 다음 시각만 계산한다."""

from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta
from pathlib import Path

from .futures_minute import DAY_OPEN, NIGHT_OPEN, is_quote_hours

BAR_INTERVAL_SEC = 60
BAR_LAG_SEC = 0.8  # 매분 0초 직후. 이전 분봉이 거래소에 닫힐 여유.
PRICE_INTERVAL_SEC = 10
URGENT_INTERVAL_SEC = 2
HEARTBEAT_IDLE_SEC = 60

CATCHUP_DAY_CLOCK = (16, 0)
CATCHUP_NIGHT_CLOCK = (6, 30)
CATCHUP_NIGHT_WINDOW_END = (8, 45)
CATCHUP_RETRY_SEC = 300
CATCHUP_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "catchup_state.json"


def next_aligned_epoch(
    interval_sec: float,
    now: datetime | None = None,
    *,
    lag_sec: float = 0.0,
) -> float:
    """now 이후 첫 정렬 시각 (unix epoch). (epoch - lag)가 interval의 배수."""
    now = now or datetime.now()
    epoch = now.timestamp()
    n = math.floor((epoch - lag_sec) / interval_sec) + 1
    nxt = n * interval_sec + lag_sec
    if nxt <= epoch + 1e-9:
        nxt += interval_sec
    return nxt


def next_clock_epoch(
    hour: int,
    minute: int,
    now: datetime | None = None,
) -> float:
    """now 이후 다음 시각(시:분:00). 이미 지났으면 내일."""
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def seconds_until(epoch: float, now: datetime | None = None) -> float:
    now = now or datetime.now()
    return max(0.05, epoch - now.timestamp())


def load_catchup_state(path: Path | None = None) -> dict[str, str]:
    path = path or CATCHUP_STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: str(data[key])
        for key in ("day", "night")
        if key in data and data[key]
    }


def save_catchup_state(state: dict[str, str], path: Path | None = None) -> None:
    path = path or CATCHUP_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def pending_catchup(
    kind: str,
    now: datetime | None = None,
    done_date: str | None = None,
) -> bool:
    """오늘 아직 안 돌렸고, 해당 세션 catch-up 창에 있으면 True.

    주간: 평일 16:00 이후(당일). 야간 장중이어도 주간 세션은 이미 닫혀 있으므로 허용.
    야간: 06:30~08:45 (일요일 오전 제외 — 토요일 야간이 없음).
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if done_date == today:
        return False
    t = now.time()
    if kind == "day":
        if now.weekday() >= 5:
            return False
        return t >= time(*CATCHUP_DAY_CLOCK)
    if kind == "night":
        if now.weekday() == 6:
            return False
        return time(*CATCHUP_NIGHT_CLOCK) <= t < time(*CATCHUP_NIGHT_WINDOW_END)
    return False


def next_catchup_epoch(
    kind: str,
    now: datetime | None = None,
    done_date: str | None = None,
) -> float:
    now = now or datetime.now()
    if pending_catchup(kind, now, done_date):
        return now.timestamp()
    hour, minute = CATCHUP_DAY_CLOCK if kind == "day" else CATCHUP_NIGHT_CLOCK
    cursor = now
    for _ in range(8):
        nxt = datetime.fromtimestamp(next_clock_epoch(hour, minute, cursor))
        if kind == "day" and nxt.weekday() >= 5:
            cursor = nxt
            continue
        if kind == "night" and nxt.weekday() == 6:
            cursor = nxt
            continue
        return nxt.timestamp()
    return next_clock_epoch(hour, minute, now)


def next_quote_open_epoch(now: datetime | None = None) -> float:
    """다음 분봉·현재가 폴링 시작 시각. 장중이면 now."""
    now = now or datetime.now()
    if is_quote_hours(now):
        return now.timestamp()
    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        candidates: list[datetime] = []
        if day.weekday() < 5:
            candidates.append(datetime.combine(day, DAY_OPEN))
        if day.weekday() != 5:
            candidates.append(datetime.combine(day, NIGHT_OPEN))
        for dt in candidates:
            if dt > now:
                return dt.timestamp()
    return now.timestamp() + 3600


def next_idle_wake_epoch(
    now: datetime | None = None,
    *,
    next_catchup_day_epoch: float | None = None,
    next_catchup_night_epoch: float | None = None,
    urgent: bool = False,
) -> float:
    """장외에서 다음 웨이크. catch-up / 다음 장 / 하트비트."""
    now = now or datetime.now()
    wakes = [
        now.timestamp() + HEARTBEAT_IDLE_SEC,
        next_quote_open_epoch(now),
    ]
    if next_catchup_day_epoch is not None:
        wakes.append(next_catchup_day_epoch)
    if next_catchup_night_epoch is not None:
        wakes.append(next_catchup_night_epoch)
    if urgent:
        wakes.append(now.timestamp() + URGENT_INTERVAL_SEC)
    return min(wakes)


def due_jobs(
    *,
    now: datetime | None = None,
    next_bar_epoch: float,
    next_price_epoch: float,
    urgent: bool,
    slack_sec: float = 0.05,
    next_catchup_day_epoch: float | None = None,
    next_catchup_night_epoch: float | None = None,
) -> list[str]:
    """이번 웨이크에서 돌릴 작업. catch-up → bars → price 순.

    분봉·현재가는 장중에만. 청산 진행(urgent)일 때만 장외 현재가 허용.
    """
    now = now or datetime.now()
    ts = now.timestamp() + slack_sec
    quote = is_quote_hours(now)
    jobs: list[str] = []
    if next_catchup_day_epoch is not None and ts >= next_catchup_day_epoch:
        jobs.append("catchup_day")
    if next_catchup_night_epoch is not None and ts >= next_catchup_night_epoch:
        jobs.append("catchup_night")
    if quote and ts >= next_bar_epoch:
        jobs.append("bars")
    if urgent or (quote and ts >= next_price_epoch):
        jobs.append("price")
    return jobs
