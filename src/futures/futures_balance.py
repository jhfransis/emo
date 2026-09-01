"""국내 선물옵션 잔고/포지션 조회."""

from __future__ import annotations

from kis_client import (
    MOCK_PROFILES,
    account_parts,
    api_get,
    issue_access_token,
    load_profile,
)

BALANCE_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-balance"
MAX_PAGES = 10


def _as_list(value) -> list[dict]:
    if value in (None, "", []):
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _has_position(row: dict) -> bool:
    try:
        return float(str(row.get("cblc_qty") or "0").replace(",", "")) > 0
    except (TypeError, ValueError):
        return False


def _tr_id(profile: str) -> str:
    return "VTFO6118R" if profile in MOCK_PROFILES else "CTFO6118R"


def fetch_futures_balance(
    profile: str,
    mgna_dvsn: str = "01",
    excc_stat_cd: str = "1",
) -> dict:
    cfg = load_profile(profile)
    appkey = cfg["appkey"]
    appsecret = cfg["seckey"]
    cano, acnt_prdt_cd = account_parts(cfg)
    token = issue_access_token(profile, appkey, appsecret)["access_token"]

    positions: list[dict] = []
    summary: dict = {}
    fk = ""
    nk = ""
    tr_cont = ""

    for _ in range(MAX_PAGES):
        data, next_cont = api_get(
            profile,
            token,
            appkey,
            appsecret,
            _tr_id(profile),
            BALANCE_PATH,
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "MGNA_DVSN": mgna_dvsn,
                "EXCC_STAT_CD": excc_stat_cd,
                "CTX_AREA_FK200": fk,
                "CTX_AREA_NK200": nk,
            },
            tr_cont=tr_cont,
        )
        if not summary:
            summary = _as_dict(data.get("output2"))
        positions.extend(row for row in _as_list(data.get("output1")) if _has_position(row))
        if next_cont in ("M", "F"):
            fk = str(data.get("ctx_area_fk200") or "")
            nk = str(data.get("ctx_area_nk200") or "")
            tr_cont = "N"
            continue
        break

    return {
        "profile": profile,
        "cano": cano,
        "acnt_prdt_cd": acnt_prdt_cd,
        "summary": summary,
        "positions": positions,
    }
