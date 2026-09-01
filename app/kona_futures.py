"""선물 잔고·트레일링 스톱 Streamlit 앱."""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from futures.futures_order import is_night_session
from futures.futures_products import is_trailable
from futures.trailing_bars import build_trail_snapshot
from futures.trailing_schedule import load_catchup_state, next_catchup_epoch
from futures.worker_launcher import start_worker, worker_process_alive
from futures import trailing_db as tdb
from ui_auth import render_logout_button, require_login
from kis_client import (
    account_label,
    get_active_profile,
    get_default_trail_points,
)

PAGE_TITLE = "KONA FUTURES"
STATUS_STOPPED = "stopped"
LIVE_REFRESH_SEC = 10
COLOR_UP = "#c0392b"
COLOR_DOWN = "#2471a3"

SUMMARY_FIELDS = [
    ("prsm_dpast_amt", "추정예탁자산"),
    ("dnca_cash", "예수금(현금)"),
    ("dnca_sbst", "예수금(대용)"),
    ("tot_dncl_amt", "총예수금"),
    ("nxdy_dnca", "익일예수금"),
    ("cash_mgna", "현금증거금"),
    ("sbst_mgna", "대용증거금"),
    ("mgna_tota", "증거금총액"),
    ("ord_psbl_cash", "주문가능현금"),
    ("ord_psbl_tota", "주문가능총액"),
    ("wdrw_psbl_tot_amt", "인출가능금액"),
    ("add_mgna_cash", "추가증거금(현금)"),
    ("add_mgna_tota", "추가증거금총액"),
    ("thdt_dfpa", "당일차금"),
    ("pchs_amt_smtl", "매입금액합계"),
    ("evlu_amt_smtl", "평가금액합계"),
    ("futr_evlu_pfls_amt", "선물평가손익"),
    ("opt_evlu_pfls_amt", "옵션평가손익"),
    ("evlu_pfls_amt_smtl", "평가손익합계"),
    ("futr_trad_pfls_amt", "선물매매손익"),
    ("opt_trad_pfls_amt", "옵션매매손익"),
    ("trad_pfls_amt_smtl", "매매손익합계"),
    ("fee", "수수료"),
]


def _to_number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt_number(value) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    if abs(number - round(number)) < 1e-9:
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def _fmt_krw(value) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    return f"{round(number):,.0f}"


def _fmt_price(value) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    return f"{number:,.2f}"


def _fmt_signed_price(value) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    return f"{number:+,.2f}"


def _flat_color() -> str:
    return "#fafafa" if _is_dark_theme() else "#212529"


def _signed_color(value) -> str:
    number = _to_number(value)
    if number is None:
        return _flat_color()
    if number > 0:
        return COLOR_UP
    if number < 0:
        return COLOR_DOWN
    return _flat_color()


def _colored(text: str, color: str, *, weight: bool = False) -> str:
    weight_css = "font-weight:600;" if weight else ""
    return f'<span style="color:{color};{weight_css}">{text}</span>'


def _fmt_qty(value) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    return f"{int(round(number)):,}"


def _pnl_html(value, *, price: bool = False) -> str:
    number = _to_number(value)
    if number is None:
        return "-"
    text = _fmt_price(number) if price else _fmt_krw(number)
    if number == 0:
        return text
    return _colored(text, _signed_color(number))


def _change_lines_html(points, entry) -> list[str]:
    number = _to_number(points)
    if number is None:
        return ["-", "-"]
    color = _signed_color(number)
    points_html = _colored(_fmt_signed_price(number), color)
    entry_num = _to_number(entry)
    if entry_num not in (None, 0):
        pct = number / entry_num * 100
        pct_html = _colored(f"({pct:+.2f}%)", color)
    else:
        pct_html = "-"
    return [points_html, pct_html]


def _side_html(text: str) -> str:
    name = str(text).strip()
    lower = name.lower()
    if "매수" in name or lower == "long":
        return _colored(html.escape(name), COLOR_UP, weight=True)
    if "매도" in name or lower == "short":
        return _colored(html.escape(name), COLOR_DOWN, weight=True)
    return html.escape(name)


def _parse_prdt_name(prdt_name: str) -> tuple[str, str]:
    name = str(prdt_name).strip()
    marker = " F "
    if marker in name:
        base, month = name.split(marker, 1)
        return base.strip(), f"F {month.strip()}"
    return name, ""


def _position_combo_label(row: dict) -> str:
    base = row.get("base_name") or str(row.get("prdt_name") or "-")
    contract = row.get("contract_label") or ""
    parts = [base]
    if contract:
        parts.append(contract)
    parts.extend([row["symbol"], row["side_label"]])
    return " / ".join(parts)


def _held_side(held: dict, trail: dict) -> str:
    side = str(held.get("side") or trail.get("side") or "").strip().lower()
    if side in ("short", "long"):
        return side
    return "long"


def _held_side_label(held: dict, side: str) -> str:
    name = str(held.get("side_label") or "").strip()
    if name:
        return name
    return "매수" if side == "long" else "매도"


def _entry_pnl(
    *,
    side: str,
    entry: float | None,
    last: float | None,
    qty: float | None,
    purchase_amt: float | None,
) -> tuple[float | None, float | None]:
    if entry is None or last is None:
        return None, None
    points = (last - entry) if side == "long" else (entry - last)
    amount = None
    if purchase_amt is not None and entry != 0 and qty is not None and qty != 0:
        amount = points * (purchase_amt / entry)
    return points, amount


def _summary_table(summary: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"항목": label, "금액": _fmt_number(summary.get(key))}
            for key, label in SUMMARY_FIELDS
        ]
    )


def _trail_map(conn) -> dict[str, dict]:
    return {row["symbol"]: row for row in tdb.list_positions(conn, include_closed=True)}


def _display_status(row: dict) -> str:
    status = str(row.get("status") or "")
    if status in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING):
        return status
    if not row.get("trailing"):
        return STATUS_STOPPED
    if status in ("idle", STATUS_STOPPED, ""):
        return "watching"
    return status


def _stop_remaining(
    side: str,
    last: float | None,
    stop: float | None,
) -> float | None:
    """현재가에서 스톱까지 남은 포인트. 0이면 즉시 청산."""
    if last is None or stop is None:
        return None
    if side == "long":
        remaining = last - stop
    else:
        remaining = stop - last
    return max(0.0, remaining)


def _build_position_row(held: dict, trail: dict) -> dict:
    symbol = str(held.get("symbol") or "").strip()
    trailable = is_trailable(symbol)
    side = _held_side(held, trail)
    entry = _to_number(held.get("entry_price") or trail.get("entry_price"))
    last = _to_number(held.get("last_price"))
    if last is None:
        last = _to_number(trail.get("last_price"))
    qty = _to_number(held.get("qty"))
    entry_pt, entry_pnl = _entry_pnl(
        side=side,
        entry=entry,
        last=last,
        qty=qty,
        purchase_amt=_to_number(held.get("purchase_amt")),
    )
    prdt_name = str(held.get("prdt_name") or trail.get("prdt_name") or "-")
    base_name, contract_label = _parse_prdt_name(prdt_name)
    stop = _to_number(trail.get("stop_price"))
    status_raw = str(trail.get("status") or "")
    close_pending = status_raw in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING)
    trailing = bool(trailable and (trail.get("enabled") or close_pending))
    return {
        "symbol": symbol,
        "trailable": trailable,
        "prdt_name": prdt_name,
        "base_name": base_name,
        "contract_label": contract_label,
        "side": side,
        "side_label": _held_side_label(held, side),
        "qty": qty,
        "lqd_qty": _to_number(held.get("lqd_qty")),
        "entry": entry,
        "last": last,
        "entry_pt": entry_pt,
        "entry_pnl": entry_pnl,
        "realized_pnl": _to_number(held.get("realized_pnl")),
        "trailing": trailing,
        "trail_points": _to_number(trail.get("trail_points")) or get_default_trail_points(),
        "extreme": _to_number(trail.get("extreme_price")),
        "stop": stop,
        "stop_remaining": _stop_remaining(side, last, stop),
        "close_pending": close_pending,
        "status": _display_status(
            {
                "trailing": trailing,
                "status": status_raw,
            }
        ),
    }


def _build_position_rows(held_rows: list[dict], trails: dict[str, dict]) -> list[dict]:
    return [
        _build_position_row(held, trails.get(str(held.get("symbol") or "").strip()) or {})
        for held in held_rows
    ]


def _load_board(conn) -> tuple[dict, list[dict]]:
    summary = tdb.get_account_summary(conn)
    trails = _trail_map(conn)
    position_rows = _build_position_rows(tdb.list_held_positions(conn), trails)
    return summary, position_rows


def _sum_open_entry_pnl(rows: list[dict]) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        pnl = row.get("entry_pnl")
        if pnl is not None:
            total += pnl
            found = True
    return total if found else None


def _sum_daily_realized_pnl(rows: list[dict]) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        pnl = row.get("realized_pnl")
        if pnl is not None:
            total += pnl
            found = True
    return total if found else None


def _is_dark_theme() -> bool:
    theme = getattr(st.context, "theme", None) or {}
    return str(theme.get("type") or "").lower() == "dark"


def _positions_table_style() -> str:
    return """
<style>
    .merge-table {
        width: 100%;
        border-collapse: collapse;
        font-family: sans-serif;
        font-size: 0.9rem;
        color: var(--st-text-color, inherit);
    }
    .merge-table th, .merge-table td {
        border: 1px solid var(--st-border-color, #dee2e6);
        padding: 10px 12px;
        text-align: center;
        vertical-align: middle;
    }
    .merge-table th {
        background-color: var(--st-secondary-background-color, #f8f9fa);
        color: #212529;
        font-weight: bold;
        line-height: 1.45;
    }
    .merge-table.merge-table-dark th,
    .merge-table.merge-table-dark td {
        border-color: var(--st-border-color, #3d3d48);
    }
    .merge-table.merge-table-dark th {
        background-color: var(--st-secondary-background-color, #262730);
        color: #fafafa;
    }
    .merged-id { font-weight: 600; line-height: 1.45; }
    .merge-table tr.row-stopped td { background-color: #e9ecef; }
    .merged-id-stopped { background-color: #e9ecef !important; }
    .merge-table.merge-table-dark tr.row-stopped td,
    .merge-table.merge-table-dark .merged-id-stopped {
        background-color: #1e1e24 !important;
    }
    .merge-table .detail-col { line-height: 1.55; }
</style>
<style>
    [data-testid="stStatusWidget"] { visibility: hidden; height: 0; }
</style>
"""

POSITION_ID_HEADER = "상품명<br>월물<br>종목코드<br>포지션"
PRICE_HEADER = "진입가<br>현재가<br>대비<br>(%)"
AMOUNT_HEADER = "당일실현손익<br>수량<br>미실현손익"
TRAIL_HEADER = "청산포지션<br>스톱가격<br>잔여 / 추적폭"


def _position_id_cell_class(row: dict) -> str:
    if not row.get("trailing"):
        return "merged-id merged-id-stopped"
    return "merged-id"


def _position_row_class(row: dict) -> str:
    if not row.get("trailing"):
        return "row-stopped"
    return ""


def _position_id_cell_html(row: dict) -> str:
    base = html.escape(str(row.get("base_name") or "-"))
    contract = html.escape(str(row.get("contract_label") or "-"))
    symbol = html.escape(row["symbol"])
    return "<br>".join([base, contract, symbol, _side_html(row.get("side_label") or "")])


def _price_side_html(price, side_label: str) -> str:
    if side_label == "매도":
        side = _colored("매도", COLOR_DOWN, weight=True)
    else:
        side = _colored("매수", COLOR_UP, weight=True)
    number = _to_number(price)
    px = _fmt_price(number) if number is not None else "-"
    return f"{side}<br>{px}"


def _plain_price_html(price) -> str:
    number = _to_number(price)
    return _fmt_price(number) if number is not None else "-"


def _entry_price_html(row: dict) -> str:
    return _plain_price_html(row.get("entry"))


def _last_price_html(row: dict) -> str:
    last = row.get("last")
    if last is None:
        return "-"
    return _colored(_fmt_price(last), _signed_color(row.get("entry_pt")), weight=True)


def _close_side_html(row: dict) -> str:
    label = "매도" if row.get("side") == "long" else "매수"
    return _side_html(label)


def _stop_price_html(row: dict) -> str:
    if not row["trailing"]:
        return "-"
    return _price_side_html(
        row.get("stop"),
        "매도" if row.get("side") == "long" else "매수",
    )


def _trail_stop_price_html(row: dict) -> str:
    return _plain_price_html(row.get("stop"))


def _trail_remaining_html(row: dict) -> str:
    remaining = _to_number(row.get("stop_remaining"))
    trail = _to_number(row.get("trail_points"))
    if remaining is None or trail is None:
        return "-"
    return f"{remaining:.1f} / {trail:.1f}"


def _position_price_cell_html(row: dict) -> str:
    return "<br>".join(
        [
            _entry_price_html(row),
            _last_price_html(row),
            *_change_lines_html(row["entry_pt"], row["entry"]),
        ]
    )


def _position_trail_cell_html(row: dict) -> str:
    if not row["trailing"]:
        return "<br>".join(["-", "-", "-"])
    return "<br>".join(
        [
            _close_side_html(row),
            _trail_stop_price_html(row),
            _trail_remaining_html(row),
        ]
    )


def _position_amount_cell_html(row: dict) -> str:
    return "<br>".join(
        [
            _pnl_html(row["realized_pnl"]),
            f"{_fmt_qty(row['qty'])} / {_fmt_qty(row['lqd_qty'])}",
            _pnl_html(row["entry_pnl"]),
        ]
    )


def _positions_merge_table_html(rows: list[dict]) -> str:
    theme_class = " merge-table-dark" if _is_dark_theme() else ""
    body = (
        f"<table class='merge-table{theme_class}'>"
        f"<tr><th>{POSITION_ID_HEADER}</th>"
        f"<th>{PRICE_HEADER}</th>"
        f"<th>{AMOUNT_HEADER}</th>"
        f"<th>{TRAIL_HEADER}</th></tr>"
    )
    for row in rows:
        row_class = _position_row_class(row)
        tr_attr = f' class="{row_class}"' if row_class else ""
        body += (
            f"<tr{tr_attr}>"
            f'<td class="{_position_id_cell_class(row)}">{_position_id_cell_html(row)}</td>'
            f'<td class="detail-col">{_position_price_cell_html(row)}</td>'
            f'<td class="detail-col">{_position_amount_cell_html(row)}</td>'
            f'<td class="detail-col">{_position_trail_cell_html(row)}</td>'
            "</tr>"
        )
    body += "</table>"
    return body


def _render_positions_table(rows: list[dict]) -> None:
    st.html(_positions_merge_table_html(rows))


def _render_strategy_section(conn, position_rows: list[dict], profile: str) -> None:
    selected_idx = st.selectbox(
        "포지션",
        options=list(range(len(position_rows))),
        format_func=lambda i: _position_combo_label(position_rows[i]),
        key="strategy_select",
    )
    _render_strategy_panel(conn, position_rows[selected_idx], profile)


def _render_strategy_panel(conn, row: dict, profile: str) -> None:
    symbol = row["symbol"]
    if not row.get("trailable"):
        st.info("KOSPI/KOSDAQ 선물만 트레일링할 수 있습니다.")
        return

    live = tdb.get_position(conn, symbol) or {}
    status = str(live.get("status") or row.get("status") or "")
    if tdb.is_close_pending(live) or status in (tdb.STATUS_TRIGGERED, tdb.STATUS_CLOSING):
        st.warning("청산 진행 중 — 체결이 끝난 뒤에 감시 중지/재시작할 수 있습니다.")
        st.caption(f"상태: {status}")
        return

    enabled = bool(live.get("enabled")) if live else bool(row.get("trailing"))
    if enabled:
        trail_points = st.number_input(
            "추적폭 (pt)",
            min_value=0.01,
            value=float(row["trail_points"]),
            step=0.05,
            format="%.2f",
            key=f"offset_{symbol}",
        )
        st.markdown(
            f"스톱 가격: {_stop_price_html(row)}",
            unsafe_allow_html=True,
        )
        with st.container(horizontal=True, gap="small", wrap=False):
            update_clicked = st.button("Update", key=f"update_{symbol}", width="stretch")
            stop_clicked = st.button("Stop", key=f"stop_{symbol}", width="stretch")
        if update_clicked:
            try:
                tdb.update_strategy(conn, symbol, trail_points=float(trail_points))
            except RuntimeError as exc:
                st.error(str(exc))
            else:
                conn.commit()
                st.rerun()
        if stop_clicked:
            if not tdb.stop_trailing(conn, symbol):
                st.error("청산 진행 중에는 감시를 중지할 수 없습니다.")
            else:
                conn.commit()
                st.rerun()
    else:
        c1, c2 = st.columns([2, 1])
        with c1:
            trail_points = st.number_input(
                "추적폭 (pt)",
                min_value=0.01,
                value=float(row["trail_points"]),
                step=0.05,
                format="%.2f",
                key=f"start_offset_{symbol}",
            )
        with c2:
            st.write("")
            if st.button("Start Trail", key=f"start_{symbol}", width="stretch"):
                last = row["last"]
                if last is None:
                    st.error("현재가를 알 수 없습니다.")
                else:
                    try:
                        snapshot = build_trail_snapshot(
                            profile,
                            symbol,
                            row["side"],
                            entry_price=row.get("entry"),
                            last_price=last,
                            trail_points=float(trail_points),
                            sync_bars=False,
                        )
                        tdb.start_trailing(
                            conn,
                            symbol,
                            trail_points=float(trail_points),
                            side=row["side"],
                            qty=float(row["qty"] or 0),
                            prdt_name=row["prdt_name"],
                            last_price=last,
                            entry_price=row.get("entry"),
                            snapshot=snapshot,
                        )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        conn.commit()
                        st.rerun()


def _events_table(events: list[dict]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(columns=["시각", "종목", "이벤트", "메시지"])
    return pd.DataFrame(
        [
            {
                "시각": row.get("created_at"),
                "종목": row.get("symbol") or "-",
                "이벤트": row.get("event"),
                "메시지": row.get("message") or "",
            }
            for row in events
        ]
    )


def _render_account_summary(summary: dict, position_rows: list[dict], account_label_text: str) -> None:
    st.markdown(f"**계좌:** {account_label_text}")

    net_asset = _fmt_number(summary.get("prsm_dpast_amt"))
    daily_realized = _to_number(summary.get("futr_trad_pfls_amt"))
    if daily_realized is None:
        daily_realized = _sum_daily_realized_pnl(position_rows)
    open_pnl = _sum_open_entry_pnl(position_rows)

    st.markdown(
        f"""
        • 추정예탁자산 **{net_asset}**  
        • 당일실현손익 **{_fmt_krw(daily_realized)}**  
        • 미실현손익 **{_fmt_krw(open_pnl)}**
        """
    )

    with st.expander("자세히", expanded=False):
        summary_df = _summary_table(summary)
        st.dataframe(summary_df, hide_index=True, width="stretch")


def _catchup_label(kind: str, now: datetime) -> str:
    state = load_catchup_state()
    epoch = next_catchup_epoch(kind, now, state.get(kind))
    if abs(epoch - now.timestamp()) < 2:
        return "지금"
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def _session_line(now: datetime | None = None) -> str:
    now = now or datetime.now()
    sess = "야간" if is_night_session(now) else "주간"
    return (
        f"세션 {sess} · 다음 catch-up 주간 {_catchup_label('day', now)} "
        f"/ 야간 {_catchup_label('night', now)}"
    )


def _worker_status_detail(conn, profile: str) -> tuple[bool, str]:
    row = tdb.get_worker_heartbeat(conn, profile)
    online = tdb.worker_is_online(conn, profile)
    if online:
        seen = str(row.get("last_seen_at") or "") if row else ""
        time_part = seen[11:19] if len(seen) >= 19 else seen or "-"
        suffix = " · dry-run" if row and row.get("dry_run") else ""
        return True, f"last tick {time_part}{suffix}"
    if worker_process_alive():
        return False, "워커 기동 중..."
    if not row:
        return False, "워커 미실행"
    seen = str(row.get("last_seen_at") or "")
    time_part = seen[11:19] if len(seen) >= 19 else seen or "-"
    suffix = " · dry-run" if row.get("dry_run") else ""
    if seen:
        try:
            seen_dt = datetime.strptime(seen, "%Y-%m-%d %H:%M:%S")
            age = int((datetime.now() - seen_dt).total_seconds())
            return False, f"last tick {time_part} ({age}s ago){suffix}"
        except ValueError:
            pass
    return False, f"last tick {time_part}{suffix}"


def _render_page_header(conn, profile: str) -> None:
    online, detail = _worker_status_detail(conn, profile)
    row = tdb.get_worker_heartbeat(conn, profile)
    dry_run = bool(row and row.get("dry_run"))
    if online:
        color = "#198754"
        label = "Online"
    else:
        color = "#6c757d"
        label = "Offline"

    title_col, status_col = st.columns([5, 1])
    with title_col:
        st.markdown(
            f'<h1 style="margin:0;padding:0;font-size:2.25rem;">{PAGE_TITLE}</h1>',
            unsafe_allow_html=True,
        )
    with status_col:
        dry_badge = ""
        if dry_run:
            dry_badge = (
                '<br><span style="color:#b45309;font-weight:600;font-size:0.78rem;">'
                "DRY-RUN</span>"
            )
        st.markdown(
            f"""
            <div style="text-align:right;line-height:1.4;padding-top:0.35rem;">
                <span style="color:{color};font-weight:600;font-size:0.95rem;">
                    ● {label}
                </span>{dry_badge}<br>
                <span style="color:#6c757d;font-size:0.78rem;">{html.escape(detail)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if not online and not worker_process_alive():
            btn_col, dry_col = st.columns(2)
            with btn_col:
                if st.button("워커 시작", key="worker_restart", width="stretch"):
                    if start_worker(profile):
                        st.toast("트레일링 워커를 시작했습니다.", icon="✅")
                        st.rerun()
                    else:
                        st.error("워커를 시작하지 못했습니다.")
            with dry_col:
                if st.button("Dry-run", key="worker_dry_run", width="stretch"):
                    if start_worker(profile, dry_run=True):
                        st.toast("Dry-run 워커를 시작했습니다.", icon="🧪")
                        st.rerun()
                    else:
                        st.error("워커를 시작하지 못했습니다.")


@st.fragment(run_every=LIVE_REFRESH_SEC)
def _render_live_board(profile: str, account_label_text: str) -> None:
    now = datetime.now()
    conn = tdb.connect(profile=profile)
    try:
        tdb.init_db(conn)
        st.caption(_session_line(now))
        summary, position_rows = _load_board(conn)
        st.subheader("Account")
        _render_account_summary(summary, position_rows, account_label_text)
        st.subheader("Positions")
        if not position_rows:
            st.info("No open positions.")
        else:
            _render_positions_table(position_rows)
    finally:
        conn.close()


@st.fragment(run_every=LIVE_REFRESH_SEC)
def _render_strategy_board(profile: str) -> None:
    conn = tdb.connect(profile=profile)
    try:
        tdb.init_db(conn)
        _, position_rows = _load_board(conn)
        trailable_rows = [row for row in position_rows if row.get("trailable")]
        if not trailable_rows:
            return
        st.subheader("Strategy")
        _render_strategy_section(conn, trailable_rows, profile)
    finally:
        conn.close()


@st.fragment(run_every=LIVE_REFRESH_SEC)
def _render_activity_board(profile: str) -> None:
    conn = tdb.connect(profile=profile)
    try:
        tdb.init_db(conn)
        st.subheader("Activity")
        with st.expander("자세히", expanded=False):
            events_df = _events_table(tdb.list_events(conn, limit=50))
            st.dataframe(events_df, hide_index=True, width="stretch")
    finally:
        conn.close()


def main() -> None:
    import importlib

    importlib.reload(tdb)

    st.set_page_config(page_title=PAGE_TITLE, layout="wide")
    st.html(_positions_table_style())
    require_login()

    profile = get_active_profile()
    label = account_label(profile)

    conn = tdb.connect(profile=profile)
    tdb.init_db(conn)
    _render_page_header(conn, profile)
    conn.close()
    _render_live_board(profile, label)
    _render_strategy_board(profile)
    _render_activity_board(profile)
    st.divider()
    render_logout_button()


if __name__ == "__main__":
    main()
