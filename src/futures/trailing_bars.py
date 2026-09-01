"""트레일링용 1분봉 종가(extreme C). futures.db 완결 봉만 사용."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from kis_client import issue_access_token, load_profile

from .futures_db import (
    DB_PATH,
    get_latest_minute_bar,
    init_db,
    list_minute_bars_after,
    sync_minute_symbols_if_needed,
)
from .futures_minute import last_possible_completed_bar
from .futures_products import SESSION_DAY, SESSION_NIGHT, product_key_for_symbol


def _completed_until(now: datetime) -> str:
    day = last_possible_completed_bar(SESSION_DAY, now)
    night = last_possible_completed_bar(SESSION_NIGHT, now)
    latest = max(day, night)
    return latest.strftime("%Y%m%d%H%M%S")


def _combine_bar_extreme(side: str, current: float | None, close: float) -> float:
    if current is None:
        return close
    if side == "long":
        return max(current, close)
    return min(current, close)


def _aggregate_closes(side: str, current: float | None, bars: list[dict]) -> float | None:
    if not bars:
        return current
    result = current
    for bar in bars:
        close = bar.get("close")
        if close is None:
            continue
        result = _combine_bar_extreme(side, result, float(close))
    return result


def refresh_bar_extreme(
    symbol: str,
    side: str,
    *,
    bar_extreme: float | None,
    bar_cursor_datetime: str | None,
    conn: sqlite3.Connection,
    session: str | None = None,
    now: datetime | None = None,
) -> tuple[float | None, str | None]:
    """futures.db의 완결 1분봉 종가로 extreme(C)와 커서를 갱신한다."""
    now = now or datetime.now()
    product_key = product_key_for_symbol(symbol)
    if not product_key:
        return bar_extreme, bar_cursor_datetime

    until = _completed_until(now)

    if not bar_cursor_datetime:
        bar = get_latest_minute_bar(
            conn,
            product_key,
            symbol,
            until_datetime=until,
        )
        if bar is None or bar.get("close") is None:
            return bar_extreme, bar_cursor_datetime
        close = float(bar["close"])
        return _combine_bar_extreme(side, bar_extreme, close), bar["trade_datetime"]

    if bar_cursor_datetime >= until:
        return bar_extreme, bar_cursor_datetime

    bars = list_minute_bars_after(
        conn,
        product_key,
        symbol,
        after_datetime=bar_cursor_datetime,
        until_datetime=until,
    )
    if not bars:
        return bar_extreme, bar_cursor_datetime

    new_extreme = _aggregate_closes(side, bar_extreme, bars)
    return new_extreme, bars[-1]["trade_datetime"]


def combine_extreme(
    side: str,
    current_price: float,
    bar_extreme: float | None,
    prev_extreme: float | None = None,
) -> float:
    """매수 max / 매도 min. 이전 extreme·현재가·분봉 종가를 누적한다. 진입가는 넣지 않는다."""
    values = [float(current_price)]
    if bar_extreme is not None:
        values.append(float(bar_extreme))
    if prev_extreme is not None:
        values.append(float(prev_extreme))
    if side == "long":
        return max(values)
    return min(values)


def compute_stop(side: str, extreme: float, trail_points: float) -> float:
    if side == "long":
        return extreme - trail_points
    return extreme + trail_points


def build_trail_snapshot(
    profile: str,
    symbol: str,
    side: str,
    *,
    entry_price: float | None,
    last_price: float,
    trail_points: float,
    token: str | None = None,
    now: datetime | None = None,
    sync_bars: bool = True,
) -> dict:
    """Start Trail 직후 extreme/stop을 즉시 계산한다 (워커 1틱 대기 없음)."""
    now = now or datetime.now()

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        if sync_bars:
            cfg = load_profile(profile)
            if token is None:
                token = issue_access_token(
                    profile, cfg["appkey"], cfg["seckey"]
                )["access_token"]
            sync_minute_symbols_if_needed(
                conn,
                profile,
                {symbol},
                token=token,
                now=now,
            )
        bar_extreme, bar_cursor = refresh_bar_extreme(
            symbol,
            side,
            bar_extreme=None,
            bar_cursor_datetime=None,
            conn=conn,
            now=now,
        )
    finally:
        conn.close()

    extreme = combine_extreme(side, last_price, bar_extreme)
    stop = compute_stop(side, extreme, trail_points)
    entry = entry_price if entry_price is not None else last_price
    return {
        "entry_price": entry,
        "bar_extreme": bar_extreme,
        "bar_cursor_datetime": bar_cursor,
        "extreme_price": extreme,
        "stop_price": stop,
        "last_price": last_price,
    }
