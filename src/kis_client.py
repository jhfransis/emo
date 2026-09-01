"""KIS Open API 공통 클라이언트."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import yaml

VPS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"
SSL_VERIFY = os.getenv("KIS_SSL_VERIFY", "1").lower() in ("1", "true", "yes")
API_INTERVAL_SEC = float(os.getenv("KIS_API_INTERVAL", "0.5"))
CFG_DIR = Path(__file__).resolve().parent.parent / "cfg"

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MOCK_PROFILES = {"mock_futures", "mock_domestic", "mock_overseas"}
DEFAULT_FUTURES_PRDT_CD = "03"
DEFAULT_ACTIVE_PROFILE = "mock_futures"
DEFAULT_TRAIL_POINTS = 5.0
PROFILE_KIND = {
    "mock_futures": "모의계좌",
    "real_futures": "실전계좌",
}
_last_api_call = 0.0


def load_config() -> dict:
    config_path = CFG_DIR / "config.yml"
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError("cfg/config.yml 형식이 올바르지 않습니다.")
    return config


def get_active_profile() -> str:
    config = load_config()
    profile = str(config.get("active_profile") or DEFAULT_ACTIVE_PROFILE).strip()
    if profile not in config or not isinstance(config[profile], dict):
        raise KeyError(f"cfg/config.yml에 active_profile={profile} 섹션이 없습니다.")
    return profile


def get_default_trail_points() -> float:
    config = load_config()
    try:
        return float(config.get("default_trail_points", DEFAULT_TRAIL_POINTS))
    except (TypeError, ValueError):
        return DEFAULT_TRAIL_POINTS


def get_ui_password() -> str | None:
    """UI 로그인 비밀번호. env KONA_UI_PASSWORD 우선, 없으면 config ui_password."""
    env = os.getenv("KONA_UI_PASSWORD", "").strip()
    if env:
        return env
    pwd = load_config().get("ui_password")
    if pwd in (None, ""):
        return None
    return str(pwd).strip() or None


def load_profile(profile: str) -> dict:
    try:
        return load_config()[profile]
    except KeyError as exc:
        raise KeyError(f"cfg/config.yml에 {profile} 섹션이 없습니다.") from exc


def account_parts(cfg: dict) -> tuple[str, str]:
    acctno = str(cfg.get("acctno", "")).replace("-", "").strip()
    prdt = str(cfg.get("acnt_prdt_cd") or "").strip()
    if not prdt and len(acctno) >= 10:
        return acctno[:8], acctno[8:10]
    return acctno[:8], prdt or DEFAULT_FUTURES_PRDT_CD


def account_label(profile: str | None = None) -> str:
    profile = profile or get_active_profile()
    cfg = load_profile(profile)
    cano, prdt = account_parts(cfg)
    kind = PROFILE_KIND.get(profile, profile)
    return f"{kind} {cano}-{prdt}"


def base_url_for(profile: str) -> str:
    return VPS_BASE_URL if profile in MOCK_PROFILES else PROD_BASE_URL


def _throttle() -> None:
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < API_INTERVAL_SEC:
        time.sleep(API_INTERVAL_SEC - elapsed)
    _last_api_call = time.time()


def _token_cache_path(profile: str) -> Path:
    return CFG_DIR / f".token_{profile}.json"


def _load_cached_token(profile: str) -> str | None:
    path = _token_cache_path(profile)
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    expires_at = data.get("expires_at", "")
    if not expires_at:
        return data.get("access_token")

    if datetime.now() >= datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S"):
        return None
    return data.get("access_token")


def _save_cached_token(profile: str, token_data: dict) -> None:
    path = _token_cache_path(profile)
    path.write_text(
        json.dumps(
            {
                "access_token": token_data["access_token"],
                "expires_at": token_data.get("access_token_token_expired", ""),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def issue_access_token(
    profile: str, appkey: str, appsecret: str, force: bool = False
) -> dict:
    if not force:
        cached = _load_cached_token(profile)
        if cached:
            return {"access_token": cached}

    _throttle()
    url = f"{base_url_for(profile)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "appsecret": appsecret,
    }
    response = requests.post(
        url, headers=headers, data=json.dumps(body), timeout=30, verify=SSL_VERIFY
    )
    response.raise_for_status()
    token_data = response.json()
    _save_cached_token(profile, token_data)
    return token_data


def _response_json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_expired_token(data: dict) -> bool:
    msg_cd = str(data.get("msg_cd") or "")
    msg1 = str(data.get("msg1") or "")
    return msg_cd == "EGW00123" or "만료된 token" in msg1


def _refresh_access_token(
    profile: str, appkey: str, appsecret: str, current_token: str
) -> str:
    cached = _load_cached_token(profile)
    if cached and cached != current_token:
        return cached
    return issue_access_token(profile, appkey, appsecret, force=True)["access_token"]


def api_get(
    profile: str,
    access_token: str,
    appkey: str,
    appsecret: str,
    tr_id: str,
    path: str,
    params: dict,
    tr_cont: str = "",
    retries: int = 4,
) -> tuple[dict, str | None]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            _throttle()
            url = f"{base_url_for(profile)}{path}"
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {access_token}",
                "appkey": appkey,
                "appsecret": appsecret,
                "tr_id": tr_id,
                "custtype": "P",
                "tr_cont": tr_cont or "",
            }
            response = requests.get(
                url, headers=headers, params=params, timeout=30, verify=SSL_VERIFY
            )
            data = _response_json(response)
            if _is_expired_token(data):
                if attempt + 1 >= retries:
                    raise RuntimeError(f"{data.get('msg_cd')}: {data.get('msg1')}")
                access_token = _refresh_access_token(
                    profile, appkey, appsecret, access_token
                )
                continue
            if response.status_code >= 500:
                raise requests.HTTPError(
                    f"{response.status_code} Server Error for url: {response.url}",
                    response=response,
                )
            response.raise_for_status()
            if data.get("rt_cd") != "0":
                raise RuntimeError(f"{data.get('msg_cd')}: {data.get('msg1')}")
            next_cont = response.headers.get("tr_cont") or response.headers.get("tr_conto")
            return data, next_cont
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except RuntimeError:
            raise
    if last_error:
        raise last_error
    raise RuntimeError("API 호출 실패")


def issue_hashkey(profile: str, appkey: str, appsecret: str, body: dict) -> str:
    _throttle()
    url = f"{base_url_for(profile)}/uapi/hashkey"
    headers = {
        "content-type": "application/json",
        "appkey": appkey,
        "appsecret": appsecret,
    }
    response = requests.post(
        url, headers=headers, data=json.dumps(body), timeout=30, verify=SSL_VERIFY
    )
    response.raise_for_status()
    data = _response_json(response)
    hashkey = data.get("HASH") or data.get("hash")
    if not hashkey:
        raise RuntimeError("hashkey 발급 실패")
    return str(hashkey)


def api_post(
    profile: str,
    access_token: str,
    appkey: str,
    appsecret: str,
    tr_id: str,
    path: str,
    body: dict,
) -> dict:
    """주문 등 POST. 타임아웃·5xx 는 재시도하지 않는다 (이미 접수됐을 수 있음).

    만료 토큰 응답만 갱신 후 1회 더 시도한다 (접수가 거절된 경우).
    """
    token_retried = False
    while True:
        hashkey = issue_hashkey(profile, appkey, appsecret, body)
        _throttle()
        url = f"{base_url_for(profile)}{path}"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {access_token}",
            "appkey": appkey,
            "appsecret": appsecret,
            "tr_id": tr_id,
            "custtype": "P",
            "hashkey": hashkey,
        }
        response = requests.post(
            url, headers=headers, data=json.dumps(body), timeout=30, verify=SSL_VERIFY
        )
        data = _response_json(response)
        if _is_expired_token(data):
            if token_retried:
                raise RuntimeError(f"{data.get('msg_cd')}: {data.get('msg1')}")
            token_retried = True
            access_token = _refresh_access_token(
                profile, appkey, appsecret, access_token
            )
            continue
        if response.status_code >= 500:
            raise requests.HTTPError(
                f"{response.status_code} Server Error for url: {response.url}",
                response=response,
            )
        response.raise_for_status()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"{data.get('msg_cd')}: {data.get('msg1')}")
        return data
