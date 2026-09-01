"""국내 선물옵션 청산 주문 (시장가) 및 체결 조회."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import requests

from kis_client import (
    MOCK_PROFILES,
    account_parts,
    api_get,
    api_post,
    issue_access_token,
    load_profile,
)

ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"
CCNL_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-ccnl"
NGT_CCNL_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-ngt-ccnl"
MAX_CCNL_PAGES = 3


class OrderUncertainError(RuntimeError):
    """주문 전송 후 성공 여부를 알 수 없음. 재전송 금지."""


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def is_night_session(now: datetime | None = None) -> bool:
    """야간파생 세션(대략 18:00~익일 06:00)."""
    now = now or datetime.now()
    t = now.time()
    return t >= time(18, 0) or t < time(6, 0)


def order_tr_id(profile: str, night: bool | None = None) -> str:
    night = is_night_session() if night is None else night
    if night:
        if profile in MOCK_PROFILES:
            raise RuntimeError("모의투자는 야간 선물 주문을 지원하지 않습니다.")
        return "STTN1101U"
    return "VTTO1101U" if profile in MOCK_PROFILES else "TTTO1101U"


def close_sll_buy_cd(side: str) -> str:
    """포지션 side에 대한 청산 주문 매매구분. 롱→매도(01), 숏→매수(02)."""
    side_norm = side.strip().lower()
    if side_norm in ("long", "buy", "매수"):
        return "01"
    if side_norm in ("short", "sell", "매도"):
        return "02"
    raise ValueError(f"알 수 없는 side: {side}")


def close_position_market(
    profile: str,
    symbol: str,
    side: str,
    qty: int | float | str,
    token: str | None = None,
) -> dict:
    """보유 포지션 시장가 전량 청산. side=long|short."""
    qty_int = int(float(qty))
    if qty_int <= 0:
        raise ValueError(f"청산 수량이 올바르지 않습니다: {qty}")

    sll_buy = close_sll_buy_cd(side)

    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]
    cano, acnt_prdt_cd = account_parts(cfg)
    if token is None:
        token = issue_access_token(profile, appkey, appsecret)["access_token"]

    tr_id = order_tr_id(profile)
    body = {
        "ORD_PRCS_DVSN_CD": "02",
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "SLL_BUY_DVSN_CD": sll_buy,
        "SHTN_PDNO": symbol,
        "ORD_QTY": str(qty_int),
        "UNIT_PRICE": "0",
        "NMPR_TYPE_CD": "02",
        "KRX_NMPR_CNDT_CD": "0",
        "ORD_DVSN_CD": "02",
    }
    try:
        data = api_post(profile, token, appkey, appsecret, tr_id, ORDER_PATH, body)
    except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
        raise OrderUncertainError(str(exc)) from exc
    output = _as_dict(data.get("output"))
    return {
        "order_no": str(output.get("ODNO") or output.get("odno") or ""),
        "ord_tmd": str(output.get("ORD_TMD") or output.get("ord_tmd") or ""),
        "raw": output,
        "tr_id": tr_id,
        "msg": str(data.get("msg1") or ""),
    }


def _as_list(value) -> list[dict]:
    if value in (None, "", []):
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _to_int(value) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def same_order_no(left, right) -> bool:
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    return a == b or a.lstrip("0") == b.lstrip("0")


def parse_order_fill(row: dict) -> dict:
    """체결내역 한 건 → 잔량/체결/거부. remaining>0 이면 거래소에 주문이 살아 있다."""
    order_no = str(row.get("odno") or row.get("ODNO") or "").strip()
    ordered = _to_int(row.get("ord_qty") or row.get("tot_ord_qty"))
    filled = _to_int(row.get("tot_ccld_qty"))
    rejected = _to_int(row.get("rjct_qty"))
    remain = _to_int(row.get("qty"))
    if "qty" not in row and ordered:
        remain = max(0, ordered - filled - rejected)
    working = remain > 0
    complete = (not working) and ordered > 0 and filled >= ordered
    failed = (not working) and not complete
    return {
        "order_no": order_no,
        "ord_qty": ordered,
        "filled_qty": filled,
        "remain_qty": remain,
        "reject_qty": rejected,
        "working": working,
        "complete": complete,
        "failed": failed,
        "raw": row,
    }


def _ccnl_date_range(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    today = now.date()
    if is_night_session(now):
        if now.time() >= time(18, 0):
            start = today
        else:
            start = today - timedelta(days=1)
        # 야간 API는 종료일을 '마지막 조회일 다음날'로 넣는다.
        end = today + timedelta(days=1)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    return today.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def _ccnl_tr(profile: str, night: bool) -> tuple[str, str]:
    if night:
        if profile in MOCK_PROFILES:
            raise RuntimeError("모의투자는 야간 체결내역을 지원하지 않습니다.")
        return "STTN5201R", NGT_CCNL_PATH
    tr_id = "VTTO5201R" if profile in MOCK_PROFILES else "TTTO5201R"
    return tr_id, CCNL_PATH


def _pdno_matches(row: dict, symbol_norm: str) -> bool:
    if not symbol_norm:
        return True
    pdno = str(row.get("pdno") or row.get("shtn_pdno") or "").strip().upper()
    if not pdno:
        return False
    return symbol_norm in pdno or pdno in symbol_norm


def _row_sll_buy_cd(row: dict) -> str:
    return str(
        row.get("sll_buy_dvsn_cd")
        or row.get("SLL_BUY_DVSN_CD")
        or ""
    ).strip()


def _row_order_datetime(row: dict) -> datetime | None:
    date_s = str(row.get("ord_dt") or row.get("ORD_DT") or "").strip()
    time_s = str(row.get("ord_tmd") or row.get("ORD_TMD") or "").strip()
    if len(date_s) != 8:
        return None
    time_s = time_s.zfill(6)[:6]
    try:
        return datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _iter_ccnl_rows(
    profile: str,
    *,
    token: str,
    now: datetime,
):
    night = is_night_session(now)
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]
    cano, acnt_prdt_cd = account_parts(cfg)
    tr_id, path = _ccnl_tr(profile, night)
    start_dt, end_dt = _ccnl_date_range(now)
    fk = ""
    nk = ""
    tr_cont = ""
    for _ in range(MAX_CCNL_PAGES):
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "STRT_ORD_DT": start_dt,
            "END_ORD_DT": end_dt,
            "SLL_BUY_DVSN_CD": "00",
            "CCLD_NCCS_DVSN": "00",
            "SORT_SQN": "DS",
            "PDNO": "",
            "STRT_ODNO": "",
            "MKET_ID_CD": "",
            "CTX_AREA_FK200": fk,
            "CTX_AREA_NK200": nk,
        }
        if night:
            params["FUOP_DVSN_CD"] = ""
            params["SCRN_DVSN"] = "02"
        data, next_cont = api_get(
            profile,
            token,
            appkey,
            appsecret,
            tr_id,
            path,
            params,
            tr_cont=tr_cont,
        )
        for row in _as_list(data.get("output1")):
            yield row
        if next_cont in ("M", "F"):
            fk = str(data.get("ctx_area_fk200") or "")
            nk = str(data.get("ctx_area_nk200") or "")
            tr_cont = "N"
            continue
        break


def fetch_order_fill(
    profile: str,
    order_no: str,
    *,
    symbol: str | None = None,
    token: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """주문번호의 체결·잔량. 아직 조회되지 않으면 None."""
    order_no = str(order_no or "").strip()
    if not order_no:
        return None

    now = now or datetime.now()
    cfg = load_profile(profile)
    if token is None:
        token = issue_access_token(
            profile, cfg["appkey"], cfg["seckey"]
        )["access_token"]
    symbol_norm = str(symbol or "").strip().upper()
    for row in _iter_ccnl_rows(profile, token=token, now=now):
        if not same_order_no(row.get("odno") or row.get("ODNO"), order_no):
            continue
        pdno = str(row.get("pdno") or row.get("shtn_pdno") or "").strip().upper()
        if symbol_norm and pdno and not _pdno_matches(row, symbol_norm):
            continue
        return parse_order_fill(row)
    return None


def find_recent_close_fill(
    profile: str,
    symbol: str,
    side: str,
    *,
    token: str | None = None,
    now: datetime | None = None,
    since: datetime | None = None,
) -> dict | None:
    """해당 종목 청산방향 체결 중 가장 최근 건(DS 정렬). since 이전이면 건너뛴다."""
    symbol_norm = str(symbol or "").strip().upper()
    if not symbol_norm:
        return None
    now = now or datetime.now()
    cfg = load_profile(profile)
    if token is None:
        token = issue_access_token(
            profile, cfg["appkey"], cfg["seckey"]
        )["access_token"]
    want_side = close_sll_buy_cd(side)
    cutoff = since - timedelta(minutes=2) if since is not None else None
    for row in _iter_ccnl_rows(profile, token=token, now=now):
        if _row_sll_buy_cd(row) != want_side:
            continue
        if not _pdno_matches(row, symbol_norm):
            continue
        if cutoff is not None:
            ordered_at = _row_order_datetime(row)
            if ordered_at is not None and ordered_at < cutoff:
                continue
        fill = parse_order_fill(row)
        if fill["order_no"]:
            return fill
    return None
