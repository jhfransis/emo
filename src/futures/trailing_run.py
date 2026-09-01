"""트레일링 워커. 분봉은 매분 0초(직후) 후 잔고, 현재가는 10초 정렬.

장중만 분봉·현재가 폴링. 세션 마감 catch-up 후 다음 장 시작까지는 대기.
세션 마감 catch-up: 16:00 주간 / 06:30 야간. 전광판 상장 월물만 증분 갱신.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from futures.trailing_engine import run_once
from futures.trailing_schedule import (
    BAR_INTERVAL_SEC,
    BAR_LAG_SEC,
    CATCHUP_DAY_CLOCK,
    CATCHUP_NIGHT_CLOCK,
    CATCHUP_RETRY_SEC,
    PRICE_INTERVAL_SEC,
    URGENT_INTERVAL_SEC,
    due_jobs,
    is_quote_hours,
    load_catchup_state,
    next_aligned_epoch,
    next_catchup_epoch,
    next_idle_wake_epoch,
    next_quote_open_epoch,
    pending_catchup,
    save_catchup_state,
    seconds_until,
)
from futures.worker_launcher import register_worker_pid_lifecycle
from futures import trailing_db as tdb
from kis_client import get_active_profile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="트레일링 스톱 워커")
    parser.add_argument(
        "--profile",
        default=None,
        help="고정 profile (미지정 시 매 틱 config active_profile 사용)",
    )
    parser.add_argument("--once", action="store_true", help="1회만 실행 (분봉+현재가)")
    parser.add_argument("--dry-run", action="store_true", help="청산 주문 없이 감시만")
    parser.add_argument(
        "--immediate",
        action="store_true",
        help="대기 없이 즉시 1회 실행 후 루프(또는 --once)",
    )
    parser.add_argument(
        "--price-interval",
        type=int,
        default=PRICE_INTERVAL_SEC,
        help=f"현재가·트레일 주기(초). 기본 {PRICE_INTERVAL_SEC}",
    )
    parser.add_argument(
        "--bar-interval",
        type=int,
        default=BAR_INTERVAL_SEC,
        help=f"분봉 갱신 주기(초). 기본 {BAR_INTERVAL_SEC} (매분 0초)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="(호환) 현재가 주기. --price-interval과 같음",
    )
    return parser.parse_args()


def _advance_bar(now: datetime, bar_interval: int) -> float:
    return next_aligned_epoch(bar_interval, now, lag_sec=BAR_LAG_SEC)


def _advance_price(now: datetime, price_interval: int) -> float:
    return next_aligned_epoch(price_interval, now, lag_sec=0.0)


def _touch_heartbeat(profile: str, dry_run: bool) -> None:
    conn = tdb.connect(profile=profile)
    try:
        tdb.touch_worker_heartbeat(conn, profile, dry_run=dry_run)
        conn.commit()
    finally:
        conn.close()


def _startup_jobs(now: datetime, catchup_state: dict[str, str]) -> tuple[str, ...]:
    if is_quote_hours(now):
        return ("bars", "price")
    jobs: list[str] = []
    if pending_catchup("day", now, catchup_state.get("day")):
        jobs.append("catchup_day")
    if pending_catchup("night", now, catchup_state.get("night")):
        jobs.append("catchup_night")
    return tuple(jobs)


def _catchup_ok(result: dict | None, kind: str) -> bool:
    if not result:
        return False
    info = ((result.get("catchup") or {}).get(kind)) or {}
    return bool(info.get("ok"))


def _advance_catchup(
    kind: str,
    jobs: tuple[str, ...],
    last_result: dict | None,
    now: datetime,
    state: dict[str, str],
    next_epoch: float,
) -> float:
    job = f"catchup_{kind}"
    if job not in jobs:
        return next_epoch
    if _catchup_ok(last_result, kind):
        state[kind] = now.strftime("%Y-%m-%d")
        save_catchup_state(state)
        nxt = next_catchup_epoch(kind, now, state.get(kind))
        print(
            f"catchup {kind} 완료 → 다음 {datetime.fromtimestamp(nxt):%Y-%m-%d %H:%M:%S}",
            flush=True,
        )
        return nxt
    if pending_catchup(kind, now, state.get(kind)):
        nxt = now.timestamp() + CATCHUP_RETRY_SEC
        print(
            f"catchup {kind} 실패 → {CATCHUP_RETRY_SEC}s 후 재시도",
            flush=True,
        )
        return nxt
    nxt = next_catchup_epoch(kind, now, state.get(kind))
    print(
        f"catchup {kind} 실패·창 종료 → 다음 {datetime.fromtimestamp(nxt):%Y-%m-%d %H:%M:%S}",
        flush=True,
    )
    return nxt


def main() -> int:
    args = parse_args()
    if not args.once:
        register_worker_pid_lifecycle()

    price_interval = max(2, int(args.interval or args.price_interval))
    bar_interval = max(5, int(args.bar_interval))
    profile = args.profile or get_active_profile()
    print(
        f"트레일링 워커 시작 profile={profile} dry_run={args.dry_run} "
        f"price={price_interval}s bar={bar_interval}s+{BAR_LAG_SEC}s "
        f"urgent={URGENT_INTERVAL_SEC}s "
        f"catchup=day {CATCHUP_DAY_CLOCK[0]:02d}:{CATCHUP_DAY_CLOCK[1]:02d} "
        f"night {CATCHUP_NIGHT_CLOCK[0]:02d}:{CATCHUP_NIGHT_CLOCK[1]:02d}"
    )

    last_result: dict | None = None
    catchup_state = load_catchup_state()
    if args.once or args.immediate:
        profile = args.profile or get_active_profile()
        startup = ("bars", "price") if args.once else _startup_jobs(datetime.now(), catchup_state)
        if startup:
            last_result = run_once(
                profile,
                dry_run=args.dry_run,
                jobs=startup,
            )
            print(last_result)
            catchup_state = load_catchup_state()
        elif args.immediate:
            print("장외 — 분봉·현재가 폴링 생략", flush=True)
        if args.once:
            return 0

    now = datetime.now()
    next_bar = _advance_bar(now, bar_interval)
    next_price = _advance_price(now, price_interval)
    next_catchup_day = next_catchup_epoch("day", now, catchup_state.get("day"))
    next_catchup_night = next_catchup_epoch("night", now, catchup_state.get("night"))
    quote_open = next_quote_open_epoch(now)
    quote_label = (
        "open"
        if is_quote_hours(now)
        else datetime.fromtimestamp(quote_open).strftime("%m-%d %H:%M")
    )
    print(
        f"catchup next day={datetime.fromtimestamp(next_catchup_day):%m-%d %H:%M:%S} "
        f"night={datetime.fromtimestamp(next_catchup_night):%m-%d %H:%M:%S} "
        f"quote={quote_label} "
        f"done={catchup_state or '-'}",
        flush=True,
    )

    while True:
        urgent = bool(last_result and last_result.get("urgent"))
        now = datetime.now()
        quote = is_quote_hours(now)
        if quote:
            wake = min(next_bar, next_price, next_catchup_day, next_catchup_night)
            if urgent:
                wake = min(wake, now.timestamp() + URGENT_INTERVAL_SEC)
        else:
            wake = next_idle_wake_epoch(
                now,
                next_catchup_day_epoch=next_catchup_day,
                next_catchup_night_epoch=next_catchup_night,
                urgent=urgent,
            )
        sleep_sec = seconds_until(wake, now)
        jobs_preview = due_jobs(
            now=datetime.fromtimestamp(now.timestamp() + sleep_sec),
            next_bar_epoch=next_bar,
            next_price_epoch=next_price,
            next_catchup_day_epoch=next_catchup_day,
            next_catchup_night_epoch=next_catchup_night,
            urgent=urgent,
        )
        print(
            f"{datetime.now():%H:%M:%S} profile={profile} "
            f"next={','.join(jobs_preview) or 'idle'} in {sleep_sec:.1f}s",
            flush=True,
        )
        time.sleep(sleep_sec)

        now = datetime.now()
        jobs = tuple(
            due_jobs(
                now=now,
                next_bar_epoch=next_bar,
                next_price_epoch=next_price,
                next_catchup_day_epoch=next_catchup_day,
                next_catchup_night_epoch=next_catchup_night,
                urgent=urgent,
            )
        )
        if not jobs:
            try:
                profile = args.profile or get_active_profile()
                _touch_heartbeat(profile, args.dry_run)
            except Exception:
                pass
            continue
        try:
            profile = args.profile or get_active_profile()
            last_result = run_once(profile, dry_run=args.dry_run, jobs=jobs)
            print(last_result, flush=True)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            last_result = None
            try:
                profile = args.profile or get_active_profile()
                _touch_heartbeat(profile, args.dry_run)
            except Exception:
                pass

        now = datetime.now()
        if "bars" in jobs:
            next_bar = _advance_bar(now, bar_interval)
        if "price" in jobs:
            next_price = _advance_price(now, price_interval)
        next_catchup_day = _advance_catchup(
            "day", jobs, last_result, now, catchup_state, next_catchup_day
        )
        next_catchup_night = _advance_catchup(
            "night", jobs, last_result, now, catchup_state, next_catchup_night
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
