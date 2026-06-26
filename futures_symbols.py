"""선물 종목코드 해석."""

from __future__ import annotations

from datetime import datetime, timedelta

from kis_client import api_get

from .futures_products import PRODUCTS, market_div_for

MARKET_FUTURES = "F"
BOARD_TR_ID = "FHPIF05030200"
BOARD_SCR_CODE = "20503"
MINUTE_TR_ID = "FHKIF03020200"
DAILY_TR_ID = "FHKIF03020100"


def resolve_near_month_code(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    board_cls: str,
    prefixes: tuple[str, ...],
) -> str:
    data, _ = api_get(
        profile,
        token,
        appkey,
        appsecret,
        BOARD_TR_ID,
        "/uapi/domestic-futureoption/v1/quotations/display-board-futures",
        {
            "FID_COND_MRKT_DIV_CODE": MARKET_FUTURES,
            "FID_COND_SCR_DIV_CODE": BOARD_SCR_CODE,
            "FID_COND_MRKT_CLS_CODE": board_cls,
        },
    )
    rows = data.get("output") or []
    if not rows:
        raise RuntimeError(f"전광판 조회 결과가 비어 있습니다. (board_cls={board_cls})")

    for key in ("futs_shrn_iscd", "shtn_pdno", "pdno", "iscd"):
        for row in rows:
            code = str(row.get(key, "")).strip()
            if any(code.startswith(prefix) for prefix in prefixes) and len(code) >= 5:
                return code

    code = str(rows[0].get("futs_shrn_iscd", "")).strip()
    if code:
        return code
    raise RuntimeError(f"근월물 종목코드를 찾지 못했습니다. (board_cls={board_cls})")


def _has_minute_data(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    symbol: str,
    market_div: str = "F",
) -> bool:
    now = datetime.now()
    try:
        data, _ = api_get(
            profile,
            token,
            appkey,
            appsecret,
            MINUTE_TR_ID,
            "/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice",
            {
                "FID_COND_MRKT_DIV_CODE": market_div,
                "FID_INPUT_ISCD": symbol,
                "FID_HOUR_CLS_CODE": "60",
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_FAKE_TICK_INCU_YN": "N",
                "FID_INPUT_DATE_1": now.strftime("%Y%m%d"),
                "FID_INPUT_HOUR_1": now.strftime("%H%M%S"),
            },
        )
        return bool(data.get("output2"))
    except Exception:
        return False


def _has_daily_data(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    symbol: str,
    market_div: str = "F",
) -> bool:
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    try:
        data, _ = api_get(
            profile,
            token,
            appkey,
            appsecret,
            DAILY_TR_ID,
            "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice",
            {
                "FID_COND_MRKT_DIV_CODE": market_div,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",
            },
        )
        return bool(data.get("output2"))
    except Exception:
        return False


def resolve_symbol(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    product_key: str,
    mode: str,
    interval: str,
) -> str:
    product = PRODUCTS[product_key]
    market_div = market_div_for(product_key)
    if mode == "continuous":
        codes = product[f"continuous_{interval}"]
        checker = _has_minute_data if interval == "1m" else _has_daily_data
        for code in codes:
            if checker(profile, token, appkey, appsecret, code, market_div):
                return code
        return codes[0]

    board_cls = product.get("board_cls")
    if not board_cls:
        raise RuntimeError(f"{product_key}는 근월물 전광판 코드가 없습니다.")
    return resolve_near_month_code(
        profile, token, appkey, appsecret, board_cls, product["prefixes"]
    )
