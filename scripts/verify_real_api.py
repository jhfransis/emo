#!/usr/bin/env python3
"""실전(real_futures) API 부분 검증 — 포지션·체결 없이 가능한 항목."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kis_client import PROD_BASE_URL, account_label, account_parts, issue_access_token, load_profile
from futures.futures_balance import fetch_futures_balance
from futures.futures_order import close_position_market, is_night_session, order_tr_id
from futures.futures_price import fetch_last_price

PROFILE = "real_futures"
# 잔고 없어도 시세 조회 가능한 대표 종목 (코스피200 202609)
PROBE_SYMBOL = "A01609"


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def _classify_order_error(exc: Exception) -> str:
    text = str(exc)
    # 장 마감·무포지션 등 정상적인 업무 거절
    business_codes = (
        "APBK0810",  # 장운영상태 아님
        "APBK0952",
        "APBK0919",
        "APBK0917",
        "APBK0916",
        "보유",
        "수량",
        "잔고",
        "주문가능",
    )
    for code in business_codes:
        if code in text:
            return "business_reject"
    auth_markers = (
        "EGW001",
        "EGW002",
        "EGW003",
        "OPSQ",
        "invalid",
        "token",
        "인증",
        "권한",
        "TR_ID",
    )
    upper = text.upper()
    for m in auth_markers:
        if m.upper() in upper:
            return "auth_or_config"
    return "business_reject"


def main() -> int:
    print(f"=== KONA real API probe ({datetime.now():%Y-%m-%d %H:%M:%S}) ===\n")
    cfg = load_profile(PROFILE)
    cano, prdt = account_parts(cfg)
    print(f"Profile: {PROFILE}")
    print(f"Account: {account_label(PROFILE)}")
    print(f"API host: {PROD_BASE_URL}")
    print(f"Night session now: {is_night_session()}\n")

    fails = 0

    # 1. Token
    print("[1] Access token")
    try:
        token = issue_access_token(PROFILE, cfg["appkey"], cfg["seckey"])["access_token"]
        _ok(f"issued (len={len(token)})")
    except Exception as exc:
        _fail(f"token: {exc}")
        return 1

    # 2. Balance
    print("\n[2] Balance inquiry")
    try:
        bal = fetch_futures_balance(PROFILE)
        summary = bal.get("summary") or {}
        positions = bal.get("positions") or []
        deposit = summary.get("ord_psbl_tota") or summary.get("tot_dncl_amt") or "-"
        _ok(f"positions={len(positions)} ord_psbl_tota={deposit}")
        for row in positions:
            sym = row.get("shtn_pdno") or row.get("pdno")
            qty = row.get("cblc_qty")
            print(f"       held {sym} qty={qty}")
    except Exception as exc:
        _fail(f"balance: {exc}")
        fails += 1

    # 3. Price
    print(f"\n[3] Price inquiry ({PROBE_SYMBOL})")
    try:
        px = fetch_last_price(PROFILE, PROBE_SYMBOL, token=token)
        _ok(f"last={px}")
    except Exception as exc:
        _fail(f"price: {exc}")
        fails += 1

    # 4. Order tr_id
    print("\n[4] Order TR_ID selection")
    day_tr = order_tr_id(PROFILE, night=False)
    night_tr = order_tr_id(PROFILE, night=True)
    _ok(f"day={day_tr} night={night_tr}")
    if day_tr != "TTTO1101U":
        _fail(f"expected TTTO1101U, got {day_tr}")
        fails += 1
    if night_tr != "STTN1101U":
        _fail(f"expected STTN1101U, got {night_tr}")
        fails += 1

    # 5. Day order API (expect business reject if no position)
    print(f"\n[5] Day close order API ({day_tr}) — no position expected")
    try:
        close_position_market(
            PROFILE, PROBE_SYMBOL, "long", 1, token=token
        )
        _warn("order accepted — unexpected without position; check manually")
    except Exception as exc:
        kind = _classify_order_error(exc)
        if kind == "auth_or_config":
            _fail(f"looks like auth/config: {exc}")
            fails += 1
        else:
            _ok(f"business reject (API path OK): {exc}")

    # 6. Night tr_id body path — only call if currently night OR force tr_id
    print(f"\n[6] Night TR_ID dry check")
    if is_night_session():
        print(f"  Night session — probing {night_tr}")
        try:
            close_position_market(
                PROFILE, PROBE_SYMBOL, "long", 1, token=token
            )
            _warn("night order accepted — unexpected")
        except Exception as exc:
            kind = _classify_order_error(exc)
            if kind == "auth_or_config":
                _fail(f"night auth/config: {exc}")
                fails += 1
            else:
                _ok(f"night business reject (API path OK): {exc}")
    else:
        _warn(
            f"주간 시간 — {night_tr} 실호출 생략 "
            "(야간 18:00~06:00에 재실행 필요)"
        )

    print("\n=== Summary ===")
    if fails:
        print(f"FAILED checks: {fails}")
        return 1
    print("All probe checks passed (체결 검증은 포지션 필요).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
