"""국내 선물 1분봉 조회."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from kis_client import api_get, issue_access_token, load_profile

from .futures_products import PRODUCTS, market_div_for
from .futures_symbols import resolve_symbol

MINUTE_TR_ID = "FHKIF03020200"
MAX_BARS_PER_CALL = 102
API_SLEEP_SEC = 0.35


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
                time.sleep(API_SLEEP_SEC * (attempt + 2))
    if last_error:
        raise last_error
    return [], None


def normalize_bar(row: dict) -> dict | None:
    date = str(row.get("stck_bsop_date", "")).strip()
    time_value = str(row.get("stck_cntg_hour", "")).strip()
    if not date or not time_value:
        return None

    time_value = time_value.zfill(6)
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


def download_minute_backward(
    profile: str,
    product_key: str,
    mode: str,
    end_dt: datetime,
    until_datetime: str | None = None,
    token: str | None = None,
    symbol: str | None = None,
    paginate: bool = True,
    verbose: bool = False,
    stop_on_empty: bool = False,
) -> tuple[str, list[dict]]:
    """end_dt부터 과거 방향으로 분봉 수집.

    until_datetime이 있으면 해당 시각(포함)까지, 없으면 서버가 더 이상 주지 않을 때까지.
    paginate=False이면 1회 호출분만 수집.
    """
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]

    product = PRODUCTS[product_key]
    market_div = market_div_for(product_key)

    if token is None:
        token = issue_access_token(profile, appkey, appsecret)["access_token"]
    if symbol is None:
        symbol = resolve_symbol(
            profile, token, appkey, appsecret, product_key, mode, "1m"
        )

    if mode == "continuous":
        symbol_candidates = list(dict.fromkeys([symbol, *product["continuous_1m"]]))
    else:
        symbol_candidates = [symbol]

    last_error: Exception | None = None
    for candidate in symbol_candidates:
        try:
            symbol, bars = _download_minute_backward_symbol(
                profile,
                appkey,
                appsecret,
                token,
                candidate,
                market_div,
                end_dt,
                until_datetime=until_datetime,
                paginate=paginate,
                verbose=verbose,
                stop_on_empty=stop_on_empty,
            )
            return symbol, bars
        except Exception as exc:
            last_error = exc
            if verbose:
                print(f"    [1m] {candidate} 실패: {exc}", flush=True)
            continue

    if last_error:
        raise last_error
    return symbol, []


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
) -> tuple[str, list[dict]]:
    date_cursor = end_dt.strftime("%Y%m%d")
    time_cursor = end_dt.strftime("%H%M%S")

    by_datetime: dict[str, dict] = {}
    page = 0

    while True:
        page += 1
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
            break

        page_bars: list[dict] = []
        page_oldest: str | None = None
        for row in rows:
            bar = normalize_bar(row)
            if not bar:
                continue
            if page_oldest is None or bar["trade_datetime"] < page_oldest:
                page_oldest = bar["trade_datetime"]
            if bar["trade_datetime"] in by_datetime:
                continue
            if until_datetime and bar["trade_datetime"] < until_datetime:
                continue
            page_bars.append(bar)

        for bar in page_bars:
            by_datetime[bar["trade_datetime"]] = bar

        if stop_on_empty and len(page_bars) == 0:
            if verbose:
                print(f"    [1m p{page}] 0건 → 중단", flush=True)
            break

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

        if len(rows) < MAX_BARS_PER_CALL:
            break

        if page_oldest is None:
            break

        oldest_in_page = min(
            (bar for bar in (normalize_bar(row) for row in rows) if bar),
            key=lambda item: item["trade_datetime"],
        )
        oldest_datetime = oldest_in_page["trade_datetime"]
        date_cursor, time_cursor = shift_cursor(
            oldest_datetime[:8], oldest_datetime[8:], 1
        )
        time.sleep(API_SLEEP_SEC)

    bars = [by_datetime[key] for key in sorted(by_datetime)]
    return symbol, bars
