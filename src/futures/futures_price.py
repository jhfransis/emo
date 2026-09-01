"""국내 선물옵션 현재가 조회."""

from __future__ import annotations

from kis_client import api_get, issue_access_token, load_profile

# inquire-price(FHMIF10000000)는 연결선물 코드(A0xxxx)에서 output이 비는 경우가 있어
# 시세호가 API의 현재가를 사용한다.
PRICE_TR_ID = "FHMIF10010000"
PRICE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-asking-price"


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def fetch_last_price(
    profile: str,
    symbol: str,
    market_div: str = "F",
    token: str | None = None,
) -> float:
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]
    if token is None:
        token = issue_access_token(profile, appkey, appsecret)["access_token"]

    data, _ = api_get(
        profile,
        token,
        appkey,
        appsecret,
        PRICE_TR_ID,
        PRICE_PATH,
        {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": symbol,
        },
    )
    output = _as_dict(data.get("output1") or data.get("output"))
    if not output:
        raise RuntimeError(f"현재가 응답이 비어 있습니다: {symbol}")

    price = _to_float(output.get("futs_prpr"))
    if price is None:
        raise RuntimeError(f"현재가를 파싱하지 못했습니다: {symbol}")
    return price
