"""선물 월물(종목) 목록·정렬·과거 월물 추정."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from kis_client import api_get

from .futures_products import DAILY_START_DATE, PRODUCTS, SESSION_NIGHT, market_div_for

BOARD_TR_ID = "FHPIF05030200"
BOARD_PATH = "/uapi/domestic-futureoption/v1/quotations/display-board-futures"
BOARD_SCR_CODE = "20503"
MARKET_FUTURES = "F"
QUARTER_END_MONTHS = (3, 6, 9, 12)
CONTRACT_MONTH_RE = re.compile(r"(20\d{4})")
HISTORY_START_MONTH = DAILY_START_DATE[:6]


@dataclass(frozen=True)
class FuturesContract:
    symbol: str
    contract_month: str  # YYYYMM
    name: str = ""


def contract_month_from_symbol(symbol: str) -> str | None:
    code = str(symbol or "").strip().upper()
    if len(code) < 6:
        return None
    year_digit = code[3]
    month = code[4:6]
    if not (year_digit.isdigit() and month.isdigit()):
        return None
    return f"202{year_digit}{month}"


def contract_month_from_row(symbol: str, row: dict | None = None) -> str:
    if row:
        name = str(row.get("hts_kor_isnm") or "")
        match = CONTRACT_MONTH_RE.search(name.replace(" ", ""))
        if match:
            return match.group(1)
    parsed = contract_month_from_symbol(symbol)
    if parsed:
        return parsed
    raise ValueError(f"월물 코드를 파싱하지 못했습니다: {symbol}")


def symbol_from_contract_month(product_key: str, contract_month: str) -> str:
    product = PRODUCTS[product_key]
    prefix = str(product.get("symbol_prefix") or product["prefixes"][0])
    year_digit = contract_month[3]
    month = contract_month[4:6]
    return f"{prefix}{year_digit}{month}"


def contract_expiry_date(contract_month: str) -> str:
    """최종 거래일(만기월 둘째 목요일)."""
    year = int(contract_month[:4])
    month = int(contract_month[4:6])
    first = datetime(year, month, 1)
    first_thursday = first + timedelta(days=(3 - first.weekday()) % 7)
    return (first_thursday + timedelta(days=7)).strftime("%Y%m%d")


def contract_end_date(contract_month: str, session: str = "day") -> str:
    """세션별 일봉 조회 상한일.

    주간은 만기일(둘째 목요일), 야간 일봉은 만기 전날이 마지막이다.
    달력 월말로 조회하면 만기 이후 구간만 잡혀 API가 빈 결과를 주고
    (`stop_on_empty`) 과거 수집이 중단된다. 야간은 마지막 봉이 하루 더
    이르므로 3·6월물처럼 주간에선 살아남는 월물도 빠질 수 있다.
    """
    expiry = contract_expiry_date(contract_month)
    if session == SESSION_NIGHT:
        return (
            datetime.strptime(expiry, "%Y%m%d") - timedelta(days=1)
        ).strftime("%Y%m%d")
    return expiry


def contract_end_datetime(contract_month: str, session: str = "day") -> datetime:
    """세션별 분봉 조회 상한 시각. 야간은 만기일 06:00(세션 종료)."""
    expiry = contract_expiry_date(contract_month)
    if session == SESSION_NIGHT:
        return datetime.strptime(expiry + "060000", "%Y%m%d%H%M%S")
    return datetime.strptime(expiry + "153000", "%Y%m%d%H%M%S")


def _matches_product(symbol: str, prefixes: tuple[str, ...]) -> bool:
    code = str(symbol or "").strip().upper()
    return any(code.startswith(prefix) for prefix in prefixes)


def _month_add(yyyymm: str, delta: int) -> str:
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6]) + delta
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return f"{year:04d}{month:02d}"


def _prev_contract_month(yyyymm: str, cycle: str) -> str:
    if cycle == "monthly":
        return _month_add(yyyymm, -1)
    month = int(yyyymm[4:6])
    year = int(yyyymm[:4])
    if month == 3:
        return f"{year - 1}12"
    if month == 6:
        return f"{year}03"
    if month == 9:
        return f"{year}06"
    if month == 12:
        return f"{year}09"
    if month > 12:
        return _prev_contract_month(f"{year}{month:02d}", cycle)
    for candidate in reversed(QUARTER_END_MONTHS):
        if candidate < month:
            return f"{year}{candidate:02d}"
    return f"{year - 1}12"


def iter_past_contract_months(from_yyyymm: str, until_yyyymm: str, cycle: str):
    cur = from_yyyymm
    while cur >= until_yyyymm:
        if cycle == "monthly" or int(cur[4:6]) in QUARTER_END_MONTHS:
            yield cur
        nxt = _prev_contract_month(cur, cycle)
        if nxt == cur:
            break
        cur = nxt


def list_board_contracts(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    product_key: str,
) -> list[FuturesContract]:
    product = PRODUCTS[product_key]
    board_cls = product.get("board_cls")
    if not board_cls:
        raise RuntimeError(f"{product_key}는 전광판 코드가 없습니다.")

    data, _ = api_get(
        profile,
        token,
        appkey,
        appsecret,
        BOARD_TR_ID,
        BOARD_PATH,
        {
            "FID_COND_MRKT_DIV_CODE": MARKET_FUTURES,
            "FID_COND_SCR_DIV_CODE": BOARD_SCR_CODE,
            "FID_COND_MRKT_CLS_CODE": board_cls,
        },
    )
    rows = data.get("output") or []
    contracts: dict[str, FuturesContract] = {}
    for row in rows:
        symbol = str(row.get("futs_shrn_iscd") or "").strip()
        if not symbol or not _matches_product(symbol, product["prefixes"]):
            continue
        month = contract_month_from_row(symbol, row)
        contracts[month] = FuturesContract(
            symbol=symbol,
            contract_month=month,
            name=str(row.get("hts_kor_isnm") or "").strip(),
        )
    return [contracts[key] for key in sorted(contracts)]


def discover_contracts(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    product_key: str,
    *,
    include_past: bool = True,
) -> list[FuturesContract]:
    """전광판 월물 + (옵션) 만기 이전 과거 월물 코드 추정."""
    board = list_board_contracts(profile, token, appkey, appsecret, product_key)
    by_month = {item.contract_month: item for item in board}
    if not include_past or not board:
        return board

    front = resolve_front_contract(
        profile, token, appkey, appsecret, product_key, board
    )
    cycle = PRODUCTS[product_key].get("contract_cycle", "quarterly")
    for month in iter_past_contract_months(
        _prev_contract_month(front.contract_month, cycle),
        HISTORY_START_MONTH,
        cycle,
    ):
        if month in by_month:
            continue
        by_month[month] = FuturesContract(
            symbol=symbol_from_contract_month(product_key, month),
            contract_month=month,
            name="",
        )
    return [by_month[key] for key in sorted(by_month)]


def _next_quarter_month(front_month: str, months: list[str]) -> str | None:
    for month in sorted(months):
        if month > front_month and int(month[4:]) in QUARTER_END_MONTHS:
            return month
    for month in sorted(months):
        if month > front_month:
            return month
    return None


def contract_sync_order(
    contracts: list[FuturesContract],
    front_month: str,
) -> list[FuturesContract]:
    """차월물(다음 분기) → 근월물 → 더 가까운 중간월물 → 과거 월물 → 먼 미래 순."""
    by_month = {item.contract_month: item for item in contracts}
    months = sorted(by_month)
    if not months:
        return []

    if front_month not in by_month:
        front_month = months[0]

    next_month = _next_quarter_month(front_month, months)
    if next_month is None:
        for month in months:
            if month > front_month:
                next_month = month
                break

    ordered_months: list[str] = []
    if next_month and next_month in by_month:
        ordered_months.append(next_month)
    ordered_months.append(front_month)

    between = sorted(
        [m for m in months if front_month < m < (next_month or "999999")],
        reverse=True,
    )
    ordered_months.extend(between)

    older = sorted([m for m in months if m < front_month], reverse=True)
    ordered_months.extend(older)

    if next_month:
        far = sorted([m for m in months if m > next_month], reverse=True)
        ordered_months.extend(far)

    seen: set[str] = set()
    result: list[FuturesContract] = []
    for month in ordered_months:
        item = by_month.get(month)
        if not item or item.symbol in seen:
            continue
        seen.add(item.symbol)
        result.append(item)
    return result


def resolve_front_contract(
    profile: str,
    token: str,
    appkey: str,
    appsecret: str,
    product_key: str,
    contracts: list[FuturesContract] | None = None,
) -> FuturesContract:
    from .futures_symbols import resolve_near_month_code

    product = PRODUCTS[product_key]
    contracts = contracts or list_board_contracts(
        profile, token, appkey, appsecret, product_key
    )
    if not contracts:
        raise RuntimeError(f"{product_key} 전광판 월물이 없습니다.")

    near_symbol = resolve_near_month_code(
        profile,
        token,
        appkey,
        appsecret,
        product["board_cls"],
        product["prefixes"],
    )
    by_symbol = {item.symbol: item for item in contracts}
    if near_symbol in by_symbol:
        return by_symbol[near_symbol]

    month = contract_month_from_symbol(near_symbol)
    if month:
        for item in contracts:
            if item.contract_month == month:
                return item
    return contracts[0]
