"""국내 선물 상품 정의."""

from __future__ import annotations

# 코스피/코스닥 일일 가격제한폭 30% 확대 시행일
DAILY_START_DATE = "20150615"
DEFAULT_MARKET_DIV = "F"
SESSION_DAY = "day"
SESSION_NIGHT = "night"
DEFAULT_SESSIONS = (SESSION_DAY, SESSION_NIGHT)
SESSION_MARKET_DIV = {
    SESSION_DAY: "F",
    SESSION_NIGHT: "CM",
}


def market_div_for(product_key: str) -> str:
    return PRODUCTS[product_key].get("market_div", DEFAULT_MARKET_DIV)


def market_div_for_session(session: str) -> str:
    return SESSION_MARKET_DIV.get(session, DEFAULT_MARKET_DIV)


PRODUCTS = {
    "kospi200_mini": {
        "label": "미니 KOSPI200 선물",
        "board_cls": "MKI",
        "prefixes": ("A05",),
        "symbol_prefix": "A05",
        "contract_cycle": "monthly",
    },
    "kospi200": {
        "label": "KOSPI200 선물",
        "board_cls": "K2I",
        "prefixes": ("A01",),
        "symbol_prefix": "A01",
        "contract_cycle": "quarterly",
    },
    "kosdaq150": {
        "label": "KOSDAQ150 선물",
        "board_cls": "KQI",
        "prefixes": ("A06",),
        "symbol_prefix": "A06",
        "contract_cycle": "quarterly",
    },
}


def trailable_prefixes() -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for product in PRODUCTS.values():
        for prefix in product["prefixes"]:
            seen.setdefault(prefix, None)
    return tuple(seen)


def is_trailable(symbol: str) -> bool:
    """KOSPI200·KOSDAQ150 선물만 트레일/표시 대상 (화이트리스트)."""
    code = str(symbol or "").strip().upper()
    if not code:
        return False
    return any(code.startswith(prefix) for prefix in trailable_prefixes())


def product_key_for_symbol(symbol: str) -> str | None:
    code = str(symbol or "").strip().upper()
    if not code:
        return None
    matches: list[tuple[int, str]] = []
    for key, product in PRODUCTS.items():
        for prefix in product["prefixes"]:
            if code.startswith(prefix):
                matches.append((len(prefix), key))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]
