"""국내 선물 일봉 조회."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from kis_client import api_get, issue_access_token, load_profile

from .futures_products import DAILY_START_DATE, market_div_for
from .futures_symbols import resolve_symbol

DAILY_TR_ID = "FHKIF03020100"
DAILY_API_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice"
CHUNK_DAYS = 20
RETRY_SLEEP_SEC = 1.0


def fetch_daily_range(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    symbol: str,
    start_date: str,
    end_date: str,
    market_div: str = "F",
    retries: int = 3,
) -> list[dict]:
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
        "FID_PERIOD_DIV_CODE": "D",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            data, _ = api_get(
                profile,
                token,
                appkey,
                appsecret,
                DAILY_TR_ID,
                DAILY_API_PATH,
                params,
            )
            return [
                bar
                for bar in (
                    normalize_daily_bar(row)
                    for row in (data.get("output2") or [])
                )
                if bar
            ]
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(RETRY_SLEEP_SEC * (attempt + 1))
    if last_error:
        raise last_error
    return []


def normalize_daily_bar(row: dict) -> dict | None:
    trade_date = str(row.get("stck_bsop_date", "")).strip()
    if not trade_date:
        return None
    return {
        "trade_date": trade_date,
        "open": _to_float(row.get("futs_oprc")),
        "high": _to_float(row.get("futs_hgpr")),
        "low": _to_float(row.get("futs_lwpr")),
        "close": _to_float(row.get("futs_prpr")),
        "volume": _to_int(row.get("acml_vol")),
    }


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _to_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _date_add(date_yyyymmdd: str, days: int) -> str:
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return dt.strftime("%Y%m%d")


def download_daily_range_data(
    profile: str,
    product_key: str,
    mode: str,
    start_date: str,
    end_date: str,
    token: str | None = None,
    symbol: str | None = None,
    verbose: bool = False,
    chunk_backward: bool = False,
    stop_on_empty: bool = False,
) -> tuple[str, list[dict]]:
    """start_date ~ end_date 구간 일봉 수집 (양끝 포함)."""
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]

    if token is None:
        token = issue_access_token(profile, appkey, appsecret)["access_token"]
    if symbol is None:
        symbol = resolve_symbol(
            profile, token, appkey, appsecret, product_key, mode, "1d"
        )

    start = max(start_date, DAILY_START_DATE)
    end = end_date
    if start > end:
        return symbol, []

    market_div = market_div_for(product_key)
    by_date: dict[str, dict] = {}

    if chunk_backward:
        cursor = end
        chunk = 0
        while cursor >= start:
            chunk += 1
            chunk_start = _date_add(cursor, -(CHUNK_DAYS - 1))
            if chunk_start < start:
                chunk_start = start
            rows = _fetch_daily_chunk(
                profile, token, appkey, appsecret, symbol, market_div,
                chunk_start, cursor, chunk, verbose,
            )
            if stop_on_empty and not rows:
                if verbose:
                    print(f"    [1d c{chunk}] 0건 → 중단", flush=True)
                break
            for row in rows:
                by_date[row["trade_date"]] = row
            cursor = _date_add(chunk_start, -1)
    else:
        cursor = start
        chunk = 0
        while cursor <= end:
            chunk += 1
            chunk_end = _date_add(cursor, CHUNK_DAYS - 1)
            if chunk_end > end:
                chunk_end = end
            rows = _fetch_daily_chunk(
                profile, token, appkey, appsecret, symbol, market_div,
                cursor, chunk_end, chunk, verbose,
            )
            for row in rows:
                by_date[row["trade_date"]] = row
            cursor = _date_add(chunk_end, 1)

    bars = [by_date[key] for key in sorted(by_date)]
    return symbol, bars


def _fetch_daily_chunk(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    symbol: str,
    market_div: str,
    chunk_start: str,
    chunk_end: str,
    chunk: int,
    verbose: bool,
) -> list[dict]:
    try:
        rows = fetch_daily_range(
            profile,
            token,
            appkey,
            appsecret,
            symbol,
            chunk_start,
            chunk_end,
            market_div=market_div,
        )
        if verbose:
            print(
                f"    [1d c{chunk}] {chunk_start}~{chunk_end} "
                f"+{len(rows)}건",
                flush=True,
            )
        return rows
    except Exception as exc:
        if verbose:
            print(
                f"    [1d c{chunk}] {chunk_start}~{chunk_end} FAIL: {exc}",
                flush=True,
            )
        return []
