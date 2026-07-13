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
SSL_VERIFY = os.getenv("KIS_SSL_VERIFY", "0").lower() in ("1", "true", "yes")
API_INTERVAL_SEC = float(os.getenv("KIS_API_INTERVAL", "0.5"))
CFG_DIR = Path(__file__).resolve().parent.parent / "cfg"

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MOCK_PROFILES = {"mock_futures", "mock_domestic", "mock_overseas"}
_last_api_call = 0.0


def load_profile(profile: str) -> dict:
    config_path = CFG_DIR / "config.yml"
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    try:
        return config[profile]
    except (TypeError, KeyError) as exc:
        raise KeyError(f"cfg/config.yml에 {profile} 섹션이 없습니다.") from exc


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


def issue_access_token(profile: str, appkey: str, appsecret: str) -> dict:
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
            if response.status_code >= 500:
                raise requests.HTTPError(
                    f"{response.status_code} Server Error for url: {response.url}",
                    response=response,
                )
            response.raise_for_status()
            data = response.json()
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
