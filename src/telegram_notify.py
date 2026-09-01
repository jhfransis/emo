"""텔레그램 개인 알림."""

from __future__ import annotations

import requests

from kis_client import PROFILE_KIND, load_config

API = "https://api.telegram.org"

_SIDE_KO = {"long": "매수", "short": "매도"}
_TITLES = {
    "triggered": "KONA 청산 발동",
    "closed": "KONA 청산 완료",
}


def telegram_config() -> dict:
    cfg = load_config().get("telegram") or {}
    if not isinstance(cfg, dict):
        return {}
    return {
        "bot_token": str(cfg.get("bot_token") or "").strip(),
        "chat_id": str(cfg.get("chat_id") or "").strip(),
        "user_chat_id": str(cfg.get("user_chat_id") or "").strip(),
    }


def destination_chat_ids(cfg: dict | None = None) -> list[str]:
    cfg = cfg or telegram_config()
    dests: list[str] = []
    for key in ("user_chat_id", "chat_id"):
        value = str(cfg.get(key) or "").strip()
        if value and value not in dests:
            dests.append(value)
    return dests


def _api(token: str, method: str, payload: dict | None = None, *, timeout: float = 8) -> dict:
    url = f"{API}/bot{token}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"telegram {method} failed")
    return data.get("result")


def _fmt_px(value) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_qty(value) -> str:
    if value in (None, ""):
        return "-"
    qty = float(value)
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    return f"{qty:g}"


def format_liquidation(
    *,
    kind: str,
    profile: str,
    symbol: str,
    product: str = "",
    prdt_name: str = "",
    side: str = "",
    qty=None,
    entry_price=None,
    last_price=None,
    stop_price=None,
    extreme_price=None,
    trail_points=None,
) -> str:
    title = _TITLES.get(kind, "KONA 알림")
    name = (prdt_name or product or symbol).strip()
    side_ko = _SIDE_KO.get(side, side or "-")
    account = PROFILE_KIND.get(profile, profile)
    lines = [
        title,
        name,
        f"{symbol} · {side_ko} {_fmt_qty(qty)}계약",
    ]
    if kind == "closed":
        lines.append(f"진입 {_fmt_px(entry_price)} → 청산 {_fmt_px(last_price)}")
    else:
        cmp_op = "≤" if side == "long" else "≥"
        lines.append(f"현재가 {_fmt_px(last_price)} {cmp_op} 스톱 {_fmt_px(stop_price)}")
    extra = []
    if kind == "closed" and stop_price not in (None, ""):
        extra.append(f"스톱 {_fmt_px(stop_price)}")
    if extreme_price not in (None, ""):
        extra.append(f"{'고점' if side == 'long' else '저점'} {_fmt_px(extreme_price)}")
    if trail_points not in (None, ""):
        extra.append(f"트레일 {_fmt_px(trail_points)}pt")
    if extra:
        lines.append(" · ".join(extra))
    footer = account
    if kind == "triggered":
        footer = f"{account} · 시장가 청산"
    lines.append(footer)
    return "\n".join(lines)


def notify_liquidation(**payload) -> None:
    try:
        send_telegram(format_liquidation(**payload))
    except Exception as exc:
        print(f"telegram notify failed: {exc}", flush=True)


def send_telegram(text: str, *, chat_id: str | None = None) -> list[dict]:
    cfg = telegram_config()
    token = cfg["bot_token"]
    if not token:
        raise RuntimeError("cfg/config.yml telegram.bot_token 이 없습니다.")
    dests = [chat_id] if chat_id else destination_chat_ids(cfg)
    if not dests:
        raise RuntimeError("cfg/config.yml telegram.user_chat_id 가 없습니다.")
    sent: list[dict] = []
    errors: list[str] = []
    for dest in dests:
        try:
            sent.append(_api(token, "sendMessage", {"chat_id": dest, "text": text}))
        except RuntimeError as exc:
            errors.append(f"{dest}: {exc}")
    if not sent:
        raise RuntimeError("; ".join(errors) or "telegram send failed")
    return sent
