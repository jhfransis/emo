#!/usr/bin/env python3
"""모의 계좌 코너케이스. 정상 발동·청산 해피패스는 넣지 않는다."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kis_client import (
    account_parts,
    api_post,
    get_active_profile,
    issue_access_token,
    load_profile,
)
from futures.futures_balance import fetch_futures_balance
from futures.futures_contracts import list_board_contracts
from futures.futures_minute import is_quote_hours
from futures.futures_order import (
    ORDER_PATH,
    OrderUncertainError,
    _as_dict,
    close_position_market,
    find_recent_close_fill,
    is_night_session,
    order_tr_id,
)
from futures.futures_price import fetch_last_price
from futures.futures_products import is_trailable, market_div_for_session
from futures.trailing_bars import combine_extreme, compute_stop
from futures.worker_launcher import worker_process_alive
from futures import trailing_db as tdb
import requests

PROFILE = "mock_futures"
BUY = "02"
SELL = "01"


def _held_side(row: dict) -> str:
    name = str(row.get("sll_buy_dvsn_name") or "")
    if "매도" in name:
        return "short"
    return "long"


def _symbol(row: dict) -> str:
    return str(row.get("shtn_pdno") or row.get("pdno") or "").strip()


def _order_body(profile: str, symbol: str, sll_buy: str, qty_int: int) -> tuple[dict, dict, str]:
    cfg = load_profile(profile)
    token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])["access_token"]
    cano, acnt = account_parts(cfg)
    body = {
        "ORD_PRCS_DVSN_CD": "02",
        "CANO": cano,
        "ACNT_PRDT_CD": acnt,
        "SLL_BUY_DVSN_CD": sll_buy,
        "SHTN_PDNO": symbol,
        "ORD_QTY": str(qty_int),
        "UNIT_PRICE": "0",
        "NMPR_TYPE_CD": "02",
        "KRX_NMPR_CNDT_CD": "0",
        "ORD_DVSN_CD": "02",
    }
    return cfg, body, token


def place_market(profile: str, symbol: str, sll_buy: str, qty: int, retries: int = 4) -> dict:
    qty_int = int(qty)
    if qty_int <= 0:
        raise ValueError(qty)
    last_exc: Exception | None = None
    for attempt in range(retries):
        cfg, body, token = _order_body(profile, symbol, sll_buy, qty_int)
        try:
            data = api_post(
                profile, token, cfg["appkey"], cfg["seckey"],
                order_tr_id(profile), ORDER_PATH, body,
            )
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_exc = OrderUncertainError(str(exc))
            time.sleep(1.5 + attempt)
            continue
        output = _as_dict(data.get("output"))
        time.sleep(1.2)
        return {
            "order_no": str(output.get("ODNO") or output.get("odno") or ""),
            "msg": str(data.get("msg1") or ""),
            "raw": output,
        }
    raise last_exc or OrderUncertainError("주문 실패")


def live_positions(profile: str) -> dict[str, dict]:
    out = {}
    for row in fetch_futures_balance(profile)["positions"]:
        symbol = _symbol(row)
        if not symbol:
            continue
        qty = float(str(row.get("cblc_qty") or "0").replace(",", "") or 0)
        if qty <= 0:
            continue
        out[symbol] = {
            "symbol": symbol,
            "side": _held_side(row),
            "qty": qty,
            "name": str(row.get("prdt_name") or ""),
            "raw": row,
        }
    return out


def flatten(profile: str) -> None:
    for pos in live_positions(profile).values():
        try:
            close_position_market(profile, pos["symbol"], pos["side"], pos["qty"])
            print(f"  flatten {pos['symbol']} {pos['side']} {pos['qty']}")
        except Exception as exc:
            print(f"  flatten fail {pos['symbol']}: {exc}")
    time.sleep(2)


def trail_row(symbol: str) -> dict | None:
    conn = tdb.connect(profile=PROFILE)
    try:
        tdb.init_db(conn)
        return tdb.get_position(conn, symbol)
    finally:
        conn.close()


def wait_until(pred, timeout: float, step: float = 2.0, label: str = "") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    print(f"  timeout {label}")
    return False


def wait_watching(symbol: str, side: str | None = None, timeout: float = 80) -> bool:
    def ok() -> bool:
        row = trail_row(symbol)
        if not row:
            return False
        if not row.get("enabled"):
            return False
        if row.get("status") != tdb.STATUS_WATCHING:
            return False
        if side and row.get("side") != side:
            return False
        return row.get("extreme_price") is not None and row.get("stop_price") is not None
    return wait_until(ok, timeout, label=f"watching {symbol}")


def pick_mini_front(profile: str) -> str:
    cfg = load_profile(profile)
    token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])["access_token"]
    board = list_board_contracts(
        profile, token, cfg["appkey"], cfg["seckey"], "kospi200_mini"
    )
    if not board:
        raise RuntimeError("미니 전광판 없음")
    return board[0].symbol


def record(results: list, name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": ok, "detail": detail})
    print(("PASS" if ok else "FAIL") + f"  {name}: {detail}")


def test_lock(results: list) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SRC / "futures" / "trailing_run.py"),
            "--once",
        ],
        cwd=str(_ROOT),
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(_SRC)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    ok = proc.returncode != 0 and "이미 실행" in (proc.stderr + proc.stdout)
    record(results, "워커 flock 중복기동 거부", ok, f"exit={proc.returncode} err={proc.stderr.strip()[:120]}")


def test_sticky_extreme(results: list) -> None:
    high = combine_extreme("long", 103.0, 100.0, prev_extreme=105.0)
    stop = compute_stop("long", high, 5.0)
    ok = high == 105.0 and abs(stop - 100.0) < 1e-9
    record(
        results,
        "틱 고점 누적(코드)",
        ok,
        f"extreme={high} stop={stop} (기대 105 / 100)",
    )


def test_night_mock_order_blocked(results: list) -> None:
    try:
        order_tr_id(PROFILE, night=True)
        record(results, "모의 야간 TR 차단", False, "예외가 나지 않음")
    except RuntimeError as exc:
        record(results, "모의 야간 TR 차단", "야간" in str(exc), str(exc))


def test_update_stop_from_extreme(results: list, symbol: str) -> None:
    row = trail_row(symbol)
    if not row or row.get("extreme_price") is None:
        record(results, "추적폭 변경이 extreme 기준", False, "watching 스냅샷 없음")
        return
    extreme = float(row["extreme_price"])
    last = float(row.get("last_price") or 0)
    conn = tdb.connect(profile=PROFILE)
    try:
        tdb.update_strategy(conn, symbol, trail_points=8.0)
        conn.commit()
        updated = tdb.get_position(conn, symbol)
    finally:
        conn.close()
    stop = float(updated["stop_price"])
    expect = compute_stop("long", extreme, 8.0)
    # last 기준이면 last-8, extreme 기준이면 extreme-8. 둘이 다를 때만 구분 가능.
    from_last = compute_stop("long", last, 8.0)
    ok = abs(stop - expect) < 1e-6
    record(
        results,
        "Update가 last가 아니라 extreme으로 스톱 재계산",
        ok,
        f"extreme={extreme} last={last} stop={stop} expect={expect} last기준={from_last}",
    )
    conn = tdb.connect(profile=PROFILE)
    try:
        tdb.update_strategy(conn, symbol, trail_points=5.0)
        conn.commit()
    finally:
        conn.close()


def test_close_guards(results: list, symbol: str) -> None:
    conn = tdb.connect(profile=PROFILE)
    try:
        row = tdb.get_position(conn, symbol)
        if not row:
            record(results, "청산중 Stop/Start 가드", False, "포지션 행 없음")
            return
        snapshot = dict(row)
        row["status"] = tdb.STATUS_CLOSING
        row["enabled"] = False
        row["order_unverified"] = 1
        row["last_order_no"] = ""
        row["close_submitted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tdb.upsert_position(conn, row)
        conn.commit()
        ignored = tdb.stop_trailing(conn, symbol) is False
        still = tdb.get_position(conn, symbol)
        start_blocked = False
        try:
            tdb.start_trailing(
                conn, symbol, trail_points=5.0, side="long", qty=1,
                prdt_name="x", last_price=float(still["last_price"] or 1),
            )
        except RuntimeError:
            start_blocked = True
        after = tdb.get_position(conn, symbol)
        ok = (
            ignored
            and start_blocked
            and after.get("status") == tdb.STATUS_CLOSING
            and after.get("last_order_no", "") == ""
        )
        record(
            results,
            "CLOSING 중 Stop 무시·Start 거부",
            ok,
            f"stop_ignored={ignored} start_blocked={start_blocked} status={after.get('status')}",
        )
        snapshot["updated_at"] = None
        tdb.upsert_position(conn, snapshot)
        conn.commit()
    finally:
        conn.close()


def test_stale_ccnl_match(results: list, symbol: str, side: str) -> None:
    fill = find_recent_close_fill(PROFILE, symbol, side, since=datetime.now())
    record(
        results,
        "직전 2분 밖 옛 체결을 현재 미확인 주문으로 안 붙임",
        fill is None or fill.get("working") is True,
        f"fill={None if fill is None else {k: fill[k] for k in ('order_no','working','complete','filled_qty')}}",
    )


def test_side_flip(results: list, symbol: str) -> None:
    place_market(PROFILE, symbol, SELL, 2)
    ok_wait = wait_watching(symbol, side="short", timeout=90)
    row = trail_row(symbol)
    pos = live_positions(PROFILE).get(symbol)
    ok = (
        ok_wait
        and pos is not None
        and pos["side"] == "short"
        and abs(pos["qty"] - 1) < 1e-6
        and row is not None
        and row.get("side") == "short"
        and row.get("status") == tdb.STATUS_WATCHING
        and not row.get("order_unverified")
    )
    record(
        results,
        "롱1 → 매도2 숏1, 청산루프에 안 묶이고 사이드 리셋",
        bool(ok),
        f"live={pos} trail_side={row.get('side') if row else None} "
        f"status={row.get('status') if row else None} extreme={row.get('extreme_price') if row else None}",
    )


def test_partial_qty(results: list, symbol: str) -> None:
    pos = live_positions(PROFILE).get(symbol)
    if not pos or pos["side"] != "short":
        record(results, "부분 청산 후 qty 동기화", False, f"숏 포지션 없음 {pos}")
        return
    # 숏 1계약에 1계약 더 팔아 2계약 → 1계약만 사서 1계약 남김
    place_market(PROFILE, symbol, SELL, 1)
    time.sleep(3)
    place_market(PROFILE, symbol, BUY, 1)
    ok_wait = wait_until(
        lambda: (
            (live_positions(PROFILE).get(symbol) or {}).get("qty") == 1
            and (trail_row(symbol) or {}).get("qty") == 1
            and (trail_row(symbol) or {}).get("side") == "short"
            and (trail_row(symbol) or {}).get("status") == tdb.STATUS_WATCHING
        ),
        timeout=90,
        label="qty=1 short watching",
    )
    row = trail_row(symbol)
    live = live_positions(PROFILE).get(symbol)
    record(
        results,
        "수량 2→1 부분청산 후 트레일은 남은 1계약만 감시",
        bool(ok_wait and live and row and row.get("enabled")),
        f"live_qty={live.get('qty') if live else None} trail_qty={row.get('qty') if row else None} "
        f"status={row.get('status') if row else None}",
    )


def wait_trail_idle(symbol: str, timeout: float = 50) -> bool:
    def ok() -> bool:
        row = trail_row(symbol)
        if not row:
            return True
        if tdb.is_close_pending(row):
            return False
        if row.get("order_unverified"):
            return False
        return True
    return wait_until(ok, timeout, step=2.0, label=f"idle {symbol}")


def force_unverified_closing(symbol: str) -> None:
    conn = tdb.connect(profile=PROFILE)
    try:
        row = tdb.get_position(conn, symbol)
        if not row:
            raise RuntimeError("force closing: no row")
        row["status"] = tdb.STATUS_CLOSING
        row["enabled"] = False
        row["order_unverified"] = 1
        row["last_order_no"] = ""
        row["close_submitted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tdb.upsert_position(conn, row)
        conn.commit()
    finally:
        conn.close()


def wait_next_bars(pad: float = 8.0) -> None:
    now = datetime.now()
    wait = 61 - now.second + pad
    if wait > 70:
        wait = pad
    print(f"  wait bars ~{wait:.0f}s")
    time.sleep(max(pad, wait))


def test_flatten_while_closing(results: list, symbol: str) -> None:
    """청산 미확인 상태에서 사람이 먼저 청산하면 워커가 반대포지션을 열면 안 됨."""
    flatten(PROFILE)
    if not wait_trail_idle(symbol):
        record(results, "청산중 수동청산 후 반대진입 없음", False, "이전 청산 상태 잔류")
        return
    place_market(PROFILE, symbol, BUY, 1)
    if not wait_watching(symbol, side="long", timeout=85):
        record(results, "청산중 수동청산 후 반대진입 없음", False, "롱 진입 실패")
        return
    force_unverified_closing(symbol)
    flatten(PROFILE)
    wait_next_bars()
    live = live_positions(PROFILE)
    row = trail_row(symbol)
    opened = bool(live)
    closed_ok = row is None or row.get("status") == tdb.STATUS_CLOSED or (
        row.get("qty") == 0 and not row.get("enabled")
    )
    record(
        results,
        "청산중 수동청산 → 워커가 반대포지션 안 염",
        (not opened) and closed_ok,
        f"live={live} status={row.get('status') if row else None} "
        f"unverified={row.get('order_unverified') if row else None} "
        f"odno={row.get('last_order_no') if row else None}",
    )


def test_reverse_while_closing(results: list, symbol: str) -> None:
    """청산 미확인 중 롱→숏 전환 시 워커가 옛 롱 청산(추가 매도)을 내면 숏이 불어남."""
    flatten(PROFILE)
    if not wait_trail_idle(symbol):
        record(results, "청산중 롱→숏 전환 시 추가매도 없이 숏1만 남음", False, "이전 청산 상태 잔류")
        return
    place_market(PROFILE, symbol, BUY, 1)
    if not wait_watching(symbol, side="long", timeout=85):
        record(results, "청산중 사이드플립 시 추가매도 없음", False, "롱 진입 실패")
        return
    force_unverified_closing(symbol)
    place_market(PROFILE, symbol, SELL, 2)
    wait_next_bars(pad=12.0)
    live = live_positions(PROFILE).get(symbol)
    row = trail_row(symbol)
    qty_ok = live is not None and live["side"] == "short" and abs(live["qty"] - 1) < 1e-6
    not_inflated = live is None or live["qty"] <= 1.01
    trail_ok = (
        row is not None
        and row.get("side") == "short"
        and row.get("status") == tdb.STATUS_WATCHING
        and not row.get("order_unverified")
    )
    record(
        results,
        "청산중 롱→숏 전환 시 추가매도 없이 숏1만 남음",
        bool(qty_ok and not_inflated and trail_ok),
        f"live={live} trail_side={row.get('side') if row else None} "
        f"status={row.get('status') if row else None} "
        f"unverified={row.get('order_unverified') if row else None} "
        f"qty={row.get('qty') if row else None}",
    )


def test_update_when_last_below_extreme(results: list, symbol: str) -> None:
    row = trail_row(symbol)
    if not row or row.get("extreme_price") is None:
        record(results, "last≠extreme 일 때 Update는 extreme 기준", False, "watching 없음")
        return
    extreme = float(row["extreme_price"])
    fake_last = extreme - 2.0
    conn = tdb.connect(profile=PROFILE)
    try:
        tdb.upsert_position(conn, {**row, "last_price": fake_last})
        conn.commit()
        tdb.update_strategy(conn, symbol, trail_points=8.0)
        conn.commit()
        updated = tdb.get_position(conn, symbol)
    finally:
        conn.close()
    stop = float(updated["stop_price"])
    expect = compute_stop("long", extreme, 8.0)
    from_last = compute_stop("long", fake_last, 8.0)
    ok = abs(stop - expect) < 1e-6 and abs(stop - from_last) > 0.5
    record(
        results,
        "last≠extreme 일 때 Update는 extreme 기준",
        ok,
        f"extreme={extreme} fake_last={fake_last} stop={stop} expect={expect} last기준={from_last}",
    )
    conn = tdb.connect(profile=PROFILE)
    try:
        tdb.update_strategy(conn, symbol, trail_points=5.0)
        conn.commit()
    finally:
        conn.close()


def test_unverified_retry_closes(results: list, symbol: str) -> None:
    row = trail_row(symbol)
    live = live_positions(PROFILE).get(symbol)
    if not row or not live:
        record(results, "미확인 주문 1분 후 재전송으로 청산", False, "포지션 없음")
        return
    conn = tdb.connect(profile=PROFILE)
    try:
        row["status"] = tdb.STATUS_CLOSING
        row["enabled"] = False
        row["order_unverified"] = 1
        row["last_order_no"] = ""
        row["close_submitted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tdb.upsert_position(conn, row)
        conn.commit()
    finally:
        conn.close()

    def done() -> bool:
        pos = live_positions(PROFILE).get(symbol)
        tr = trail_row(symbol)
        if pos is None:
            return True
        if tr and str(tr.get("last_order_no") or "").strip():
            return True
        if tr and tr.get("status") == tdb.STATUS_CLOSED:
            return True
        return False

    wait_until(done, timeout=95, step=3, label="unverified retry")
    time.sleep(8)
    pos = live_positions(PROFILE).get(symbol)
    tr = trail_row(symbol)
    flat = pos is None
    closing_sent = bool(tr and str(tr.get("last_order_no") or "").strip())
    stuck = (
        not flat
        and tr
        and tr.get("status") == tdb.STATUS_CLOSING
        and tr.get("order_unverified")
        and not str(tr.get("last_order_no") or "").strip()
    )
    record(
        results,
        "미확인(주문번호 없음) → 다음 1분에 재전송 또는 잔고 소멸",
        flat or closing_sent,
        f"flat={flat} order_no={tr.get('last_order_no') if tr else None} "
        f"status={tr.get('status') if tr else None} unverified={tr.get('order_unverified') if tr else None} stuck={stuck}",
    )


def main() -> int:
    print(f"=== mock corner tests {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    active = get_active_profile()
    if active != PROFILE:
        print(f"active_profile={active}, mock_futures 가 아니라서 중단")
        return 2
    if is_night_session():
        print("야간이라 모의 주문을 넣을 수 없음")
        return 2
    if not is_quote_hours():
        print("장외 — 주문 테스트 생략")
        return 2
    if not worker_process_alive():
        print("워커가 없음")
        return 2

    results: list[dict] = []
    races_only = "--races-only" in sys.argv
    if not races_only:
        test_lock(results)
        test_sticky_extreme(results)
        test_night_mock_order_blocked(results)

    symbol = pick_mini_front(PROFILE)
    px = fetch_last_price(PROFILE, symbol, market_div=market_div_for_session("day"))
    print(f"symbol {symbol} last={px}")
    flatten(PROFILE)

    def run_step(name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            record(results, name, False, f"exception: {type(exc).__name__}: {exc}")
            flatten(PROFILE)

    try:
        if races_only:
            run_step("청산중 수동청산 → 워커가 반대포지션 안 염", lambda: test_flatten_while_closing(results, symbol))
            run_step("청산중 롱→숏 전환 시 추가매도 없이 숏1만 남음", lambda: test_reverse_while_closing(results, symbol))
        else:
            place_market(PROFILE, symbol, BUY, 1)
            if not wait_watching(symbol, side="long", timeout=85):
                record(results, "롱 진입 후 auto-trail (전제)", False, str(trail_row(symbol)))
            else:
                record(results, "롱 진입 후 auto-trail (전제)", True, str({
                    k: trail_row(symbol).get(k)
                    for k in ("status", "side", "trail_points", "extreme_price", "stop_price")
                }))
                test_update_when_last_below_extreme(results, symbol)
                test_close_guards(results, symbol)
                test_stale_ccnl_match(results, symbol, "long")
                run_step("롱1 → 매도2 숏1, 청산루프에 안 묶이고 사이드 리셋", lambda: test_side_flip(results, symbol))
                run_step("수량 2→1 부분청산 후 트레일은 남은 1계약만 감시", lambda: test_partial_qty(results, symbol))
                run_step("미확인(주문번호 없음) → 다음 1분에 재전송 또는 잔고 소멸", lambda: test_unverified_retry_closes(results, symbol))
                flatten(PROFILE)
                run_step("청산중 수동청산 → 워커가 반대포지션 안 염", lambda: test_flatten_while_closing(results, symbol))
                run_step("청산중 롱→숏 전환 시 추가매도 없이 숏1만 남음", lambda: test_reverse_while_closing(results, symbol))
    finally:
        flatten(PROFILE)
        time.sleep(5)

    print("\n--- summary ---")
    failed = 0
    for row in results:
        if not row["ok"]:
            failed += 1
        print(f"{'PASS' if row['ok'] else 'FAIL'}  {row['name']}")
    print(f"{len(results) - failed}/{len(results)} passed")
    leftover = live_positions(PROFILE)
    print("leftover", leftover)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
