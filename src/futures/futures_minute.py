"""국내 선물 1분봉 조회."""

from __future__ import annotations

import time as time_mod
from datetime import datetime, time, timedelta

from kis_client import api_get, issue_access_token, load_profile

from .futures_products import (
    SESSION_DAY,
    SESSION_NIGHT,
    market_div_for_session,
)

MINUTE_TR_ID = "FHKIF03020200"
MAX_BARS_PER_CALL = 102
API_SLEEP_SEC = 0.35


def completed_cutoff(now: datetime | None = None) -> datetime:
    """마지막 완결 1분봉 시각. 12:03:30이면 12:02:00."""
    now = now or datetime.now()
    return now.replace(second=0, microsecond=0) - timedelta(minutes=1)


DAY_OPEN = time(8, 45)
DAY_CLOSE_BAR = time(15, 45)
NIGHT_OPEN = time(18, 0)
NIGHT_CLOSE_BAR = time(6, 0)
# 마지막 완결봉(15:45/06:00)을 15:46:00.8 / 06:01:00.8 에 받기 위한 여유.
_QUOTE_POLL_AFTER_CLOSE = timedelta(minutes=1, seconds=1)
# 휴장·세션갭·체결 없는 구간에서 빈 페이지가 나와도 이전 세션으로 건너뛴다.
MAX_EMPTY_SESSION_SKIPS = 40
NIGHT_EMPTY_STEP = timedelta(hours=6)


def is_day_quote_hours(now: datetime | None = None) -> bool:
    """주간 정규장 분봉·현재가 폴링 구간. 평일 08:45~15:46."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    start = datetime.combine(now.date(), DAY_OPEN)
    end = datetime.combine(now.date(), DAY_CLOSE_BAR) + _QUOTE_POLL_AFTER_CLOSE
    return start <= now < end


def is_night_quote_hours(now: datetime | None = None) -> bool:
    """야간장 분봉·현재가 폴링 구간.

    월~금 18:00~익일 06:01 (금→토 포함), 일 18:00~월 06:01.
    토요일 저녁·일요일 오전은 없다.
    """
    now = now or datetime.now()
    t = now.time()
    if t >= NIGHT_OPEN:
        return now.weekday() != 5
    end = datetime.combine(now.date(), NIGHT_CLOSE_BAR) + _QUOTE_POLL_AFTER_CLOSE
    if now < end:
        return now.weekday() != 6
    return False


def is_quote_hours(now: datetime | None = None) -> bool:
    """분봉·현재가 API를 폴링해야 하는 장중(마지막 봉 수신 직후까지)."""
    now = now or datetime.now()
    return is_day_quote_hours(now) or is_night_quote_hours(now)


def current_session(now: datetime | None = None) -> str:
    """야간파생 18:00~06:00이면 night, 그 외(주간·장후 갭)는 day."""
    now = now or datetime.now()
    t = now.time()
    if t >= NIGHT_OPEN or t < NIGHT_CLOSE_BAR:
        return SESSION_NIGHT
    return SESSION_DAY


def last_possible_completed_bar(
    session: str,
    now: datetime | None = None,
) -> datetime:
    """해당 세션에서 지금 시점에 DB에 들어올 수 있는 마지막 완결 1분봉 시각."""
    now = now or datetime.now()
    cutoff = completed_cutoff(now)
    if session == SESSION_NIGHT:
        return _last_night_bar_at_or_before(cutoff)
    return _last_day_bar_at_or_before(cutoff)


def session_open_datetime(session: str, now: datetime | None = None) -> datetime:
    """해당 세션의 최근 개장 시각. catch-up 시 DB가 비어 있으면 여기까지만 페이지한다."""
    now = now or datetime.now()
    if session == SESSION_NIGHT:
        if now.time() >= NIGHT_OPEN:
            return datetime.combine(now.date(), NIGHT_OPEN)
        return datetime.combine(now.date() - timedelta(days=1), NIGHT_OPEN)
    d = now.date()
    if now.time() < DAY_OPEN:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, DAY_OPEN)


def _last_day_bar_at_or_before(cutoff: datetime) -> datetime:
    d = cutoff.date()
    t = cutoff.time()
    if d.weekday() >= 5:
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return datetime.combine(d, DAY_CLOSE_BAR)
    if t > DAY_CLOSE_BAR:
        return datetime.combine(d, DAY_CLOSE_BAR)
    if t < DAY_OPEN:
        prev = d - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        return datetime.combine(prev, DAY_CLOSE_BAR)
    return cutoff.replace(second=0, microsecond=0)


def _last_night_bar_at_or_before(cutoff: datetime) -> datetime:
    t = cutoff.time()
    if t >= NIGHT_OPEN or t <= NIGHT_CLOSE_BAR:
        return cutoff.replace(second=0, microsecond=0)
    return datetime.combine(cutoff.date(), NIGHT_CLOSE_BAR)


def _step_back_empty_cursor(
    date_yyyymmdd: str, time_hhmmss: str, *, night: bool
) -> tuple[str, str]:
    """빈 페이지 다음 조회 커서. 야간은 주간 갭을 건너 이전 06:00으로."""
    dt = datetime.strptime(
        f"{date_yyyymmdd}{time_hhmmss.zfill(6)}", "%Y%m%d%H%M%S"
    )
    if night:
        if dt.time() == NIGHT_CLOSE_BAR:
            stepped = datetime.combine(dt.date() - timedelta(days=1), NIGHT_CLOSE_BAR)
        else:
            stepped = dt - NIGHT_EMPTY_STEP
            if NIGHT_CLOSE_BAR < stepped.time() < NIGHT_OPEN:
                stepped = datetime.combine(stepped.date(), NIGHT_CLOSE_BAR)
        return stepped.strftime("%Y%m%d"), stepped.strftime("%H%M%S")

    prev = dt.date() - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev.strftime("%Y%m%d"), DAY_CLOSE_BAR.strftime("%H%M%S")


def fetch_minute_page(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    symbol: str,
    date_yyyymmdd: str,
    time_hhmmss: str,
    market_div: str = "F",
    include_past: str = "Y",
    retries: int = 3,
) -> tuple[list[dict], dict | None]:
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_INPUT_ISCD": symbol,
        "FID_HOUR_CLS_CODE": "60",
        "FID_PW_DATA_INCU_YN": include_past,
        "FID_FAKE_TICK_INCU_YN": "N",
        "FID_INPUT_DATE_1": date_yyyymmdd,
        "FID_INPUT_HOUR_1": time_hhmmss,
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            data, _ = api_get(
                profile,
                token,
                appkey,
                appsecret,
                MINUTE_TR_ID,
                "/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice",
                params,
            )
            output1 = data.get("output1")
            meta = output1[0] if isinstance(output1, list) and output1 else output1
            return data.get("output2") or [], meta if isinstance(meta, dict) else None
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time_mod.sleep(API_SLEEP_SEC * (attempt + 2))
    if last_error:
        raise last_error
    return [], None


def normalize_bar(row: dict, *, night: bool = False) -> dict | None:
    date = str(row.get("stck_bsop_date", "")).strip()
    time_value = str(row.get("stck_cntg_hour", "")).strip()
    if not date or not time_value:
        return None

    date, time_value = normalize_session_time(date, time_value, night=night)
    return {
        "trade_datetime": f"{date}{time_value}",
        "trade_date": date,
        "trade_time": time_value,
        "open": _to_float(row.get("futs_oprc")),
        "high": _to_float(row.get("futs_hgpr")),
        "low": _to_float(row.get("futs_lwpr")),
        "close": _to_float(row.get("futs_prpr")),
        "volume": _to_int(row.get("cntg_vol")),
    }


def normalize_session_time(
    trade_date: str, trade_time: str, *, night: bool
) -> tuple[str, str]:
    """야간(CM) 30xxxx 시각을 익일 06:00 이후 실시간으로 변환."""
    time_value = str(trade_time).strip().zfill(6)
    if not night:
        return trade_date, time_value
    hour = int(time_value) // 10000
    if hour >= 24:
        rest = int(time_value) % 10000
        real_time = (hour - 24) * 10000 + rest
        next_date = (
            datetime.strptime(trade_date, "%Y%m%d") + timedelta(days=1)
        ).strftime("%Y%m%d")
        return next_date, f"{real_time:06d}"
    return trade_date, time_value


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _to_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def shift_cursor(date_yyyymmdd: str, time_hhmmss: str, minutes: int) -> tuple[str, str]:
    dt = datetime.strptime(f"{date_yyyymmdd}{time_hhmmss.zfill(6)}", "%Y%m%d%H%M%S")
    dt -= timedelta(minutes=minutes)
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M%S")


def download_minute_for_symbol(
    profile: str,
    product_key: str,
    symbol: str,
    end_dt: datetime,
    until_datetime: str | None = None,
    token: str | None = None,
    paginate: bool = True,
    verbose: bool = False,
    stop_on_empty: bool = False,
    *,
    session: str = "day",
) -> tuple[str, list[dict]]:
    """지정 월물(symbol) 1분봉을 end_dt부터 과거 방향으로 수집."""
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]
    market_div = market_div_for_session(session)

    if token is None:
        token = issue_access_token(profile, appkey, appsecret)["access_token"]

    return _download_minute_backward_symbol(
        profile,
        appkey,
        appsecret,
        token,
        symbol,
        market_div,
        end_dt,
        until_datetime=until_datetime,
        paginate=paginate,
        verbose=verbose,
        stop_on_empty=stop_on_empty,
        night=(session == "night"),
    )


def _download_minute_backward_symbol(
    profile: str,
    appkey: str,
    appsecret: str,
    token: str,
    symbol: str,
    market_div: str,
    end_dt: datetime,
    until_datetime: str | None,
    paginate: bool,
    verbose: bool,
    stop_on_empty: bool,
    *,
    night: bool = False,
) -> tuple[str, list[dict]]:
    cutoff = completed_cutoff()
    cutoff_key = cutoff.strftime("%Y%m%d%H%M%S")
    if end_dt > cutoff:
        end_dt = cutoff

    date_cursor = end_dt.strftime("%Y%m%d")
    time_cursor = end_dt.strftime("%H%M%S")

    by_datetime: dict[str, dict] = {}
    page = 0
    empty_skips = 0
    seen_data = False
    max_pages = 25000

    while True:
        page += 1
        if page > max_pages:
            if verbose:
                print(f"    [1m] page cap {max_pages} → 중단", flush=True)
            break
        try:
            rows, _meta = fetch_minute_page(
                profile,
                token,
                appkey,
                appsecret,
                symbol,
                date_cursor,
                time_cursor,
                market_div=market_div,
            )
        except Exception:
            if by_datetime:
                break
            raise

        if not rows:
            if not paginate:
                break
            empty_skips += 1
            if until_datetime:
                cursor_key = f"{date_cursor}{time_cursor.zfill(6)}"
                if cursor_key <= until_datetime:
                    break
            elif stop_on_empty and seen_data and empty_skips > MAX_EMPTY_SESSION_SKIPS:
                if verbose:
                    print(f"    [1m p{page}] 0건 → 중단", flush=True)
                break
            elif stop_on_empty and not seen_data and empty_skips > MAX_EMPTY_SESSION_SKIPS:
                if verbose:
                    print(f"    [1m p{page}] 0건 → 중단", flush=True)
                break
            elif not stop_on_empty and not until_datetime:
                break
            nxt_date, nxt_time = _step_back_empty_cursor(
                date_cursor, time_cursor, night=night
            )
            if (nxt_date, nxt_time) == (date_cursor, time_cursor):
                break
            if verbose:
                print(
                    f"    [1m p{page}] {date_cursor} {time_cursor} 0건 → "
                    f"{nxt_date} {nxt_time} skip",
                    flush=True,
                )
            date_cursor, time_cursor = nxt_date, nxt_time
            time_mod.sleep(API_SLEEP_SEC)
            continue

        page_bars: list[dict] = []
        page_oldest: str | None = None
        for row in rows:
            bar = normalize_bar(row, night=night)
            if not bar:
                continue
            if page_oldest is None or bar["trade_datetime"] < page_oldest:
                page_oldest = bar["trade_datetime"]
            if bar["trade_datetime"] > cutoff_key:
                continue
            if bar["trade_datetime"] in by_datetime:
                continue
            if until_datetime and bar["trade_datetime"] < until_datetime:
                continue
            page_bars.append(bar)

        for bar in page_bars:
            by_datetime[bar["trade_datetime"]] = bar
        if page_bars:
            seen_data = True
            empty_skips = 0

        if verbose:
            oldest = page_oldest or "-"
            newest = max((bar["trade_datetime"] for bar in page_bars), default="-")
            print(
                f"    [1m p{page}] {date_cursor} {time_cursor} "
                f"+{len(page_bars)}건 (누적 {len(by_datetime)}건, {oldest}~{newest})",
                flush=True,
            )

        if until_datetime and page_oldest and page_oldest <= until_datetime:
            break

        if not paginate:
            break

        parsed = [
            bar
            for bar in (normalize_bar(row, night=night) for row in rows)
            if bar
        ]
        if not parsed:
            nxt_date, nxt_time = _step_back_empty_cursor(
                date_cursor, time_cursor, night=night
            )
            if (nxt_date, nxt_time) == (date_cursor, time_cursor):
                break
            date_cursor, time_cursor = nxt_date, nxt_time
            time_mod.sleep(API_SLEEP_SEC)
            continue

        oldest_in_page = min(parsed, key=lambda item: item["trade_datetime"])
        oldest_datetime = oldest_in_page["trade_datetime"]
        nxt_date, nxt_time = shift_cursor(
            oldest_datetime[:8], oldest_datetime[8:], 1
        )
        if (nxt_date, nxt_time) == (date_cursor, time_cursor) or not page_bars:
            nxt_date, nxt_time = _step_back_empty_cursor(
                date_cursor, time_cursor, night=night
            )
        if (nxt_date, nxt_time) == (date_cursor, time_cursor):
            break
        date_cursor, time_cursor = nxt_date, nxt_time
        time_mod.sleep(API_SLEEP_SEC)

    bars = [by_datetime[key] for key in sorted(by_datetime)]
    return symbol, bars
