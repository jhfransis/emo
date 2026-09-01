"""트레일링 스톱 엔진 (1틱). 스톱 도달 시 시장가 청산, 수동 Stop은 감시만 중단."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from kis_client import (
    get_active_profile,
    get_default_trail_points,
    issue_access_token,
    load_profile,
)

from .futures_balance import fetch_futures_balance
from .futures_db import DB_PATH, catchup_listed_session, init_db, sync_minute_symbols_if_needed
from .futures_minute import current_session
from .futures_order import (
    OrderUncertainError,
    close_position_market,
    fetch_order_fill,
    find_recent_close_fill,
    is_night_session,
)
from .futures_price import fetch_last_price
from .futures_products import (
    PRODUCTS,
    SESSION_DAY,
    SESSION_NIGHT,
    is_trailable,
    market_div_for_session,
    product_key_for_symbol,
)
from telegram_notify import notify_liquidation
from .trailing_bars import build_trail_snapshot, combine_extreme, refresh_bar_extreme
from . import trailing_db as tdb


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _side_from_balance(row: dict) -> str:
    name = str(row.get("sll_buy_dvsn_name") or "").strip()
    if "매도" in name:
        return "short"
    if "매수" in name:
        return "long"
    code = str(row.get("sll_buy_dvsn_cd") or "").strip()
    if code == "01":
        return "short"
    if code == "02":
        return "long"
    raise ValueError(f"매매구분을 알 수 없습니다: {row}")


def _symbol_from_balance(row: dict) -> str:
    for key in ("shtn_pdno", "pdno"):
        symbol = str(row.get(key) or "").strip()
        if symbol:
            return symbol
    raise ValueError(f"종목코드를 알 수 없습니다: {row}")


def _compute_stop(side: str, extreme: float, trail_points: float) -> float:
    if side == "long":
        return extreme - trail_points
    return extreme + trail_points


def _is_triggered(side: str, price: float, stop: float) -> bool:
    if side == "long":
        return price <= stop
    return price >= stop


def _entry_from_balance(row: dict) -> float | None:
    return _to_float(row.get("ccld_avg_unpr1"))


def _product_label(symbol: str) -> str:
    key = product_key_for_symbol(symbol)
    if not key:
        return ""
    return str(PRODUCTS[key].get("label") or "")


def _side_label_from_balance(row: dict, side: str) -> str:
    name = str(row.get("sll_buy_dvsn_name") or "").strip()
    if name:
        return name
    return "매수" if side == "long" else "매도"


def _held_from_kis(row: dict) -> dict:
    symbol = _symbol_from_balance(row)
    side = _side_from_balance(row)
    return {
        "symbol": symbol,
        "prdt_name": str(row.get("prdt_name") or "").strip(),
        "side": side,
        "side_label": _side_label_from_balance(row, side),
        "qty": _to_float(row.get("cblc_qty")),
        "lqd_qty": _to_float(row.get("lqd_psbl_qty")),
        "entry_price": _entry_from_balance(row),
        "purchase_amt": _to_float(row.get("pchs_amt")),
        "realized_pnl": _to_float(row.get("trad_pfls_amt")),
        "last_price": _to_float(row.get("idx_clpr")),
        "raw": row,
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def _trail_live_from_held(held: dict) -> dict:
    qty = held.get("lqd_qty")
    if qty is None or float(qty) <= 0:
        qty = held.get("qty") or 0
    return {
        "symbol": held["symbol"],
        "prdt_name": held.get("prdt_name") or "",
        "side": held["side"],
        "qty": float(qty or 0),
        "entry_price": held.get("entry_price"),
        "raw": held.get("raw") or {},
    }


def _needed_bar_symbols(conn) -> set[str]:
    symbols: set[str] = set()
    for row in tdb.list_held_positions(conn):
        if is_trailable(row["symbol"]):
            symbols.add(row["symbol"])
    for row in tdb.list_positions(conn):
        if row.get("status") == tdb.STATUS_CLOSED:
            continue
        if is_trailable(row["symbol"]):
            symbols.add(row["symbol"])
    return symbols


def _has_urgent(conn) -> bool:
    return any(
        row.get("status") in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING)
        for row in tdb.list_positions(conn)
    )


def _apply_balance_snapshot(
    conn,
    profile: str,
    *,
    save_summary: bool,
) -> tuple[dict[str, dict], dict[str, dict]]:
    balance = fetch_futures_balance(profile)
    if save_summary:
        tdb.save_account_summary(conn, balance.get("summary") or {})
    all_live: dict[str, dict] = {}
    trailable_live: dict[str, dict] = {}
    held_rows: list[dict] = []
    for row in balance.get("positions") or []:
        try:
            held = _held_from_kis(row)
        except ValueError as exc:
            tdb.add_event(conn, "sync_skip", str(exc))
            continue
        live = _trail_live_from_held(held)
        all_live[held["symbol"]] = live
        held_rows.append(held)
        if is_trailable(held["symbol"]):
            trailable_live[held["symbol"]] = live
    tdb.replace_held_positions(conn, held_rows)
    return all_live, trailable_live


def _notify_liquidation(kind: str, profile: str, row: dict) -> None:
    notify_liquidation(
        kind=kind,
        profile=profile,
        symbol=str(row.get("symbol") or ""),
        product=_product_label(str(row.get("symbol") or "")),
        prdt_name=str(row.get("prdt_name") or ""),
        side=str(row.get("side") or ""),
        qty=row.get("qty"),
        entry_price=row.get("entry_price"),
        last_price=row.get("last_price"),
        stop_price=row.get("stop_price"),
        extreme_price=row.get("extreme_price"),
        trail_points=row.get("trail_points"),
    )


def _fetch_price(profile: str, symbol: str, session: str, token: str) -> float:
    return fetch_last_price(
        profile,
        symbol,
        market_div=market_div_for_session(session),
        token=token,
    )


def _compute_watch_extreme(
    profile: str,
    symbol: str,
    side: str,
    row: dict,
    live: dict,
    *,
    token: str,
    fut_conn: sqlite3.Connection,
    session: str,
    now: datetime,
    last_price: float | None = None,
) -> tuple[float, float, float | None, str | None, float | None]:
    """b=현재가, c=1분봉 종가 누적 → extreme 및 bar 상태 반환."""
    entry_price = _entry_from_balance(live["raw"]) or _to_float(row.get("entry_price"))
    bar_extreme, bar_cursor = refresh_bar_extreme(
        symbol,
        side,
        bar_extreme=_to_float(row.get("bar_extreme")),
        bar_cursor_datetime=row.get("bar_cursor_datetime"),
        conn=fut_conn,
        session=session,
        now=now,
    )
    price = last_price if last_price is not None else _fetch_price(
        profile, symbol, session, token
    )
    extreme = combine_extreme(
        side,
        price,
        bar_extreme,
        prev_extreme=_to_float(row.get("extreme_price")),
    )
    return extreme, price, bar_extreme, bar_cursor, entry_price


def _refresh_last_price_only(
    conn,
    profile: str,
    row: dict,
    live: dict,
    *,
    token: str,
    session: str,
    last_price: float | None = None,
) -> None:
    """트레일이 꺼진 보유 종목도 현재가만 가격 주기로 갱신."""
    try:
        price = (
            last_price
            if last_price is not None
            else _fetch_price(profile, live["symbol"], session, token)
        )
    except Exception as exc:
        tdb.add_event(conn, "price_error", str(exc), symbol=live["symbol"])
        return
    tdb.upsert_position(
        conn,
        {
            **row,
            "last_price": price,
            "qty": live["qty"],
            "entry_price": _entry_from_balance(live["raw"]) or row.get("entry_price"),
            "enabled": False,
            "status": tdb.STATUS_STOPPED,
        },
    )


def _bootstrap_auto_trail(
    conn,
    profile: str,
    live: dict,
    *,
    token: str,
    trail_points: float,
    session: str,
) -> None:
    """신규·재진입 포지션을 기본 Offset으로 자동 trailing 시작."""
    symbol = live["symbol"]
    last_price = _fetch_price(profile, symbol, session, token)
    snapshot = build_trail_snapshot(
        profile,
        symbol,
        live["side"],
        entry_price=live.get("entry_price"),
        last_price=last_price,
        trail_points=trail_points,
        token=token,
    )
    tdb.start_trailing(
        conn,
        symbol,
        trail_points=trail_points,
        side=live["side"],
        qty=live["qty"],
        prdt_name=live["prdt_name"],
        last_price=last_price,
        entry_price=live.get("entry_price"),
        snapshot=snapshot,
    )
    tdb.add_event(
        conn,
        "auto_trail_start",
        f"offset={trail_points} {live['side']} qty={live['qty']}",
        symbol=symbol,
    )


def _close_qty(live: dict, fill: dict | None = None) -> float:
    live_qty = float(live.get("qty") or 0)
    if fill:
        remain = float(fill.get("remain_qty") or 0)
        if remain > 0:
            if live_qty > 0:
                return min(live_qty, remain)
            return remain
    return live_qty


def _is_unverified(row: dict) -> bool:
    return bool(row.get("order_unverified"))


def _close_submitted_at(row: dict) -> datetime | None:
    raw = str(row.get("close_submitted_at") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _advance_close(
    conn,
    profile: str,
    row: dict,
    live: dict,
    *,
    token: str,
    dry_run: bool,
    actions: list[dict],
    allow_unverified_resubmit: bool = False,
) -> None:
    """청산: 미확인·살아있는 주문은 조회만. 1분 잔고에서 미체결이 확인된 뒤에만 재주문."""
    symbol = row["symbol"]
    if dry_run:
        tdb.upsert_position(
            conn,
            {
                **row,
                "qty": live["qty"],
                "enabled": False,
                "status": tdb.STATUS_STOPPED,
                "order_unverified": False,
                "close_submitted_at": None,
            },
        )
        tdb.add_event(conn, "dry_run", "청산 주문 생략", symbol=symbol)
        return

    if is_night_session() and profile.startswith("mock_"):
        tdb.upsert_position(
            conn,
            {
                **row,
                "qty": live["qty"],
                "status": tdb.STATUS_ERROR,
                "order_unverified": False,
            },
        )
        tdb.add_event(
            conn,
            "order_skip",
            "모의투자는 야간 주문을 지원하지 않습니다",
            symbol=symbol,
        )
        return

    order_no = str(row.get("last_order_no") or "").strip()
    unverified = _is_unverified(row)
    fill: dict | None = None
    retrying = False

    if order_no:
        try:
            fill = fetch_order_fill(
                profile,
                order_no,
                symbol=symbol,
                token=token,
            )
        except Exception as exc:
            tdb.add_event(conn, "order_lookup_error", str(exc), symbol=symbol)
            return
        if fill is None:
            return
        if fill["working"] or fill["complete"]:
            if unverified:
                tdb.upsert_position(
                    conn,
                    {
                        **row,
                        "order_unverified": False,
                        "qty": live["qty"],
                    },
                )
            return
        tdb.add_event(
            conn,
            "order_incomplete",
            (
                f"order_no={order_no} filled={fill['filled_qty']}/"
                f"{fill['ord_qty']} remain={fill['remain_qty']} "
                f"reject={fill['reject_qty']} → 잔량 재주문"
            ),
            symbol=symbol,
        )
        row = {
            **row,
            "order_unverified": False,
            "last_order_no": "",
        }
    elif unverified:
        try:
            fill = find_recent_close_fill(
                profile,
                symbol,
                str(row.get("side") or live.get("side") or ""),
                token=token,
                since=_close_submitted_at(row),
            )
        except Exception as exc:
            tdb.add_event(conn, "order_lookup_error", str(exc), symbol=symbol)
            return
        if fill is None:
            if not allow_unverified_resubmit:
                return
            tdb.add_event(
                conn,
                "order_unverified_retry",
                "1분 잔고·체결에 청산 주문 없음 → 재전송",
                symbol=symbol,
            )
            row = {
                **row,
                "order_unverified": False,
                "last_order_no": "",
            }
            retrying = True
        elif fill["working"] or fill["complete"]:
            tdb.upsert_position(
                conn,
                {
                    **row,
                    "last_order_no": fill["order_no"],
                    "order_unverified": False,
                    "qty": live["qty"],
                },
            )
            tdb.add_event(
                conn,
                "order_reconciled",
                f"order_no={fill['order_no']} working={fill['working']} complete={fill['complete']}",
                symbol=symbol,
            )
            return
        else:
            if not allow_unverified_resubmit:
                return
            tdb.add_event(
                conn,
                "order_unverified_retry",
                f"order_no={fill['order_no']} 거부/소멸 → 재전송",
                symbol=symbol,
            )
            row = {
                **row,
                "order_unverified": False,
                "last_order_no": "",
            }
            retrying = True

    qty = _close_qty(live, fill)
    if qty <= 0:
        tdb.add_event(conn, "order_skip", "재주문 수량 없음", symbol=symbol)
        return

    _submit_close_order(
        conn,
        profile,
        {**row, "qty": qty},
        {**live, "qty": qty},
        token=token,
        actions=actions,
        event_kind="order_retry" if order_no or retrying else "order_sent",
    )


def _submit_close_order(
    conn,
    profile: str,
    row: dict,
    live: dict,
    *,
    token: str,
    actions: list[dict],
    event_kind: str = "order_sent",
) -> None:
    """시장가 청산 1회. 전송 전에 미확인으로 표시하고, 응답이 불확실하면 재전송하지 않는다."""
    symbol = row["symbol"]
    pending = {
        **row,
        "qty": live["qty"],
        "enabled": False,
        "status": tdb.STATUS_CLOSING,
        "order_unverified": True,
        "close_submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tdb.upsert_position(conn, pending)
    conn.commit()
    try:
        result = close_position_market(
            profile,
            symbol,
            row["side"],
            live["qty"],
            token=token,
        )
        new_no = str(result.get("order_no") or "").strip()
        tdb.upsert_position(
            conn,
            {
                **pending,
                "last_order_no": new_no,
                "order_unverified": not bool(new_no),
            },
        )
        tdb.add_event(
            conn,
            event_kind,
            f"order_no={new_no or '(없음)'} msg={result.get('msg')}",
            symbol=symbol,
        )
        actions.append(
            {
                "symbol": symbol,
                "action": "order_sent",
                "order_no": new_no,
            }
        )
    except OrderUncertainError as exc:
        tdb.upsert_position(conn, pending)
        tdb.add_event(
            conn,
            "order_uncertain",
            f"전송 결과 불명 — 재전송 금지: {exc}",
            symbol=symbol,
        )
        actions.append({"symbol": symbol, "action": "order_uncertain", "error": str(exc)})
    except Exception as exc:
        tdb.upsert_position(
            conn,
            {**pending, "order_unverified": False},
        )
        tdb.add_event(conn, "order_error", str(exc), symbol=symbol)
        actions.append({"symbol": symbol, "action": "order_error", "error": str(exc)})


def _sync_trail_from_live(
    conn,
    profile: str,
    all_live_by_symbol: dict[str, dict],
    trailable_live: dict[str, dict],
    *,
    token: str,
    session: str,
    default_trail: float,
) -> None:
    existing = {
        row["symbol"]: row
        for row in tdb.list_positions(conn, include_closed=True)
    }

    for symbol, prev in existing.items():
        if prev.get("status") == tdb.STATUS_CLOSED:
            continue
        if is_trailable(symbol):
            continue
        tdb.upsert_position(
            conn,
            {
                **prev,
                "enabled": False,
                "status": tdb.STATUS_CLOSED,
            },
        )
        tdb.add_event(
            conn,
            "ignored",
            "KOSPI/KOSDAQ 선물만 대상 (화이트리스트)",
            symbol=symbol,
        )

    for symbol, live in trailable_live.items():
        prev = existing.get(symbol)
        if prev is None or prev.get("status") == tdb.STATUS_CLOSED:
            try:
                _bootstrap_auto_trail(
                    conn,
                    profile,
                    live,
                    token=token,
                    trail_points=default_trail,
                    session=session,
                )
                tdb.add_event(
                    conn,
                    "synced",
                    f"{live['side']} qty={live['qty']} trailing=on (auto)",
                    symbol=symbol,
                )
            except Exception as exc:
                tdb.add_event(conn, "bootstrap_error", str(exc), symbol=symbol)
            continue

        side_changed = prev["side"] != live["side"]
        updates = {
            **prev,
            "prdt_name": live["prdt_name"] or prev.get("prdt_name"),
            "side": live["side"],
            "qty": live["qty"],
            "entry_price": live.get("entry_price") or prev.get("entry_price"),
        }
        if side_changed and tdb.is_close_pending(prev):
            updates["side"] = prev["side"]
            tdb.add_event(
                conn,
                "side_changed_ignored",
                f"{prev['side']} -> {live['side']} 청산 중이라 무시",
                symbol=symbol,
            )
        elif side_changed:
            updates["bar_extreme"] = None
            updates["bar_cursor_datetime"] = None
            updates["extreme_price"] = None
            updates["stop_price"] = None
            updates["last_price"] = _fetch_price(
                profile,
                symbol,
                session,
                token,
            )
            updates["status"] = tdb.STATUS_WATCHING
            updates["last_order_no"] = ""
            tdb.add_event(
                conn,
                "side_changed",
                f"{prev['side']} -> {live['side']} reset bar cursor",
                symbol=symbol,
            )
        elif prev.get("status") in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING):
            if symbol in trailable_live:
                updates["qty"] = live["qty"]
        elif prev.get("status") == tdb.STATUS_ERROR:
            pass
        elif prev.get("enabled"):
            updates["status"] = tdb.STATUS_WATCHING
        else:
            updates["status"] = tdb.STATUS_STOPPED
        tdb.upsert_position(conn, updates)

    for symbol, prev in existing.items():
        if prev.get("status") == tdb.STATUS_CLOSED:
            continue
        if not is_trailable(symbol):
            continue
        if symbol not in all_live_by_symbol:
            trail_close = prev.get("status") in (
                tdb.STATUS_TRIGGERED,
                tdb.STATUS_CLOSING,
            )
            tdb.close_position_from_balance(conn, prev)
            if trail_close:
                _notify_liquidation("closed", profile, prev)


def _run_price_tick(
    conn,
    fut_conn,
    profile: str,
    *,
    token: str,
    session: str,
    now: datetime,
    dry_run: bool,
    actions: list[dict],
    allow_unverified_resubmit: bool = False,
) -> list[dict]:
    held_rows = tdb.list_held_positions(conn)
    held_by_symbol = {row["symbol"]: row for row in held_rows}
    prices: dict[str, float] = {}
    for held in held_rows:
        symbol = held["symbol"]
        try:
            price = _fetch_price(profile, symbol, session, token)
        except Exception as exc:
            tdb.add_event(conn, "price_error", str(exc), symbol=symbol)
            continue
        prices[symbol] = price
        tdb.update_held_last_price(conn, symbol, price)

    watching = [
        row
        for row in tdb.list_positions(conn)
        if row.get("status") != tdb.STATUS_CLOSED and is_trailable(row["symbol"])
    ]
    for row in watching:
        symbol = row["symbol"]
        status = row.get("status")
        closing_pending = status in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING)
        held = held_by_symbol.get(symbol)
        if held is None:
            continue
        live = _trail_live_from_held(held)
        if closing_pending:
            _advance_close(
                conn,
                profile,
                row,
                live,
                token=token,
                dry_run=dry_run,
                actions=actions,
                allow_unverified_resubmit=allow_unverified_resubmit,
            )
            continue

        price = prices.get(symbol)
        if not row.get("enabled"):
            if price is not None:
                _refresh_last_price_only(
                    conn,
                    profile,
                    row,
                    live,
                    token=token,
                    session=session,
                    last_price=price,
                )
            continue
        if price is None:
            continue

        try:
            extreme, price, bar_extreme, bar_cursor, entry_price = (
                _compute_watch_extreme(
                    profile,
                    symbol,
                    row["side"],
                    row,
                    live,
                    token=token,
                    fut_conn=fut_conn,
                    session=session,
                    now=now,
                    last_price=price,
                )
            )
        except Exception as exc:
            tdb.upsert_position(
                conn,
                {**row, "status": tdb.STATUS_ERROR},
            )
            tdb.add_event(conn, "price_error", str(exc), symbol=symbol)
            continue

        trail_points = float(row["trail_points"])
        side = row["side"]
        stop = _compute_stop(side, extreme, trail_points)
        triggered = _is_triggered(side, price, stop)

        updated = {
            **row,
            "entry_price": entry_price,
            "bar_extreme": bar_extreme,
            "bar_cursor_datetime": bar_cursor,
            "extreme_price": extreme,
            "stop_price": stop,
            "last_price": price,
            "qty": live["qty"],
            "status": tdb.STATUS_TRIGGERED if triggered else tdb.STATUS_WATCHING,
        }
        if not triggered:
            tdb.upsert_position(conn, updated)
            continue

        tdb.upsert_position(conn, updated)
        tdb.add_event(
            conn,
            "triggered",
            f"px={price} stop={stop} extreme={extreme} trail={trail_points}",
            symbol=symbol,
        )
        actions.append({"symbol": symbol, "action": "trigger", "price": price})
        if row.get("status") == tdb.STATUS_WATCHING:
            _notify_liquidation("triggered", profile, updated)
        _advance_close(
            conn,
            profile,
            updated,
            live,
            token=token,
            dry_run=dry_run,
            actions=actions,
            allow_unverified_resubmit=allow_unverified_resubmit,
        )
    return watching


def _result_payload(
    *,
    profile: str,
    session: str,
    jobs: tuple[str, ...],
    live_positions: int,
    watching: int,
    actions: list[dict],
    minute_sync: list[dict],
    catchup: dict,
    urgent: bool,
    now: datetime,
) -> dict:
    fetched = [item for item in minute_sync if not item.get("skipped")]
    return {
        "profile": profile,
        "session": session,
        "jobs": list(jobs),
        "live_positions": live_positions,
        "watching": watching,
        "actions": actions,
        "minute_sync": {
            "fetched": len(fetched),
            "skipped": len(minute_sync) - len(fetched),
            "saved": sum(int(item.get("saved") or 0) for item in minute_sync),
        },
        "catchup": catchup,
        "urgent": urgent,
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def run_once(
    profile: str | None = None,
    *,
    dry_run: bool = False,
    db_path=None,
    jobs: tuple[str, ...] = ("bars", "price"),
) -> dict:
    """한 웨이크. jobs에 catchup_day/night, bars, price를 넣는다.

    bars: 분봉 동기화 후 잔고/자산 스냅샷을 DB에 저장.
    price: 보유 종목 현재가만 조회해 DB에 저장하고 트레일을 감시.
    """
    do_catchup_day = "catchup_day" in jobs
    do_catchup_night = "catchup_night" in jobs
    do_bars = "bars" in jobs
    do_price = "price" in jobs
    profile = profile or get_active_profile()
    default_trail = get_default_trail_points()
    now = datetime.now()
    session = current_session(now)

    conn = tdb.connect(db_path, profile=profile)
    fut_conn = sqlite3.connect(DB_PATH)
    try:
        tdb.init_db(conn)
        init_db(fut_conn)

        need_token = do_catchup_day or do_catchup_night or do_bars
        if do_price and (
            tdb.list_held_positions(conn) or _has_urgent(conn)
        ):
            need_token = True
        token: str | None = None
        if need_token:
            cfg = load_profile(profile)
            token = issue_access_token(
                profile, cfg["appkey"], cfg["seckey"]
            )["access_token"]

        catchup: dict[str, dict] = {}
        if do_catchup_day:
            if token is None:
                raise RuntimeError("catch-up에 토큰이 필요합니다")
            catchup["day"] = catchup_listed_session(
                fut_conn,
                profile,
                SESSION_DAY,
                token=token,
                now=now,
            )
        if do_catchup_night:
            if token is None:
                raise RuntimeError("catch-up에 토큰이 필요합니다")
            catchup["night"] = catchup_listed_session(
                fut_conn,
                profile,
                SESSION_NIGHT,
                token=token,
                now=now,
            )

        minute_sync: list[dict] = []
        if do_bars:
            if token is None:
                raise RuntimeError("분봉/잔고 조회에 토큰이 필요합니다")
            minute_sync = sync_minute_symbols_if_needed(
                fut_conn,
                profile,
                _needed_bar_symbols(conn),
                token=token,
                now=now,
            )
            all_live, trailable_live = _apply_balance_snapshot(
                conn, profile, save_summary=True
            )
            _sync_trail_from_live(
                conn,
                profile,
                all_live,
                trailable_live,
                token=token,
                session=session,
                default_trail=default_trail,
            )
        elif do_price and _has_urgent(conn):
            if token is None:
                raise RuntimeError("긴급 잔고 조회에 토큰이 필요합니다")
            all_live, trailable_live = _apply_balance_snapshot(
                conn, profile, save_summary=False
            )
            _sync_trail_from_live(
                conn,
                profile,
                all_live,
                trailable_live,
                token=token,
                session=session,
                default_trail=default_trail,
            )

        actions: list[dict] = []
        watching: list[dict] = []
        if do_price and token is not None:
            watching = _run_price_tick(
                conn,
                fut_conn,
                profile,
                token=token,
                session=session,
                now=now,
                dry_run=dry_run,
                actions=actions,
                allow_unverified_resubmit=do_bars,
            )

        tdb.touch_worker_heartbeat(conn, profile, dry_run=dry_run)
        conn.commit()
        return _result_payload(
            profile=profile,
            session=session,
            jobs=jobs,
            live_positions=len(tdb.list_held_positions(conn)),
            watching=len(watching),
            actions=actions,
            minute_sync=minute_sync,
            catchup=catchup,
            urgent=_has_urgent(conn),
            now=now,
        )
    finally:
        fut_conn.close()
        conn.close()
