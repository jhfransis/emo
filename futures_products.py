"""국내 선물 상품 정의."""

from __future__ import annotations

# 코스피/코스닥 일일 가격제한폭 30% 확대 시행일
DAILY_START_DATE = "20150615"
DEFAULT_MARKET_DIV = "F"


def market_div_for(product_key: str) -> str:
    return PRODUCTS[product_key].get("market_div", DEFAULT_MARKET_DIV)


PRODUCTS = {
    "kospi200": {
        "label": "KOSPI200 선물",
        "table_1m": "kospi200_1m",
        "table_1d": "kospi200_1d",
        "board_cls": "MKI",
        "prefixes": ("101", "A05", "A01"),
        "continuous_1m": ("A0100", "10100"),
        "continuous_1d": ("A0100", "10100"),
    },
    "kosdaq150": {
        "label": "KOSDAQ150 선물",
        "table_1m": "kosdaq150_1m",
        "table_1d": "kosdaq150_1d",
        "board_cls": "KQI",
        "prefixes": ("106", "A06"),
        "continuous_1m": ("A0600", "10600"),
        "continuous_1d": ("A0600", "10600"),
    },
}
