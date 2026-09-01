"""Streamlit UI 접근 제어 (단일 비밀번호 + URL 세션)."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta

import streamlit as st

from kis_client import get_ui_password
from ui_session import SESSION_HOURS, create_session, delete_session, validate_session

QUERY_SID_KEY = "sid"


def _sid_from_query() -> str | None:
    sid = st.query_params.get(QUERY_SID_KEY)
    if not sid:
        return None
    sid = str(sid).strip()
    return sid or None


def _apply_session(sid: str, expires_at: datetime) -> None:
    st.session_state.authenticated = True
    st.session_state.auth_sid = sid
    st.session_state.auth_expires_at = expires_at


def _restore_session_from_query() -> bool:
    if st.session_state.get("authenticated"):
        return True

    sid = _sid_from_query()
    expires_at = validate_session(sid)
    if expires_at is None:
        if sid and QUERY_SID_KEY in st.query_params:
            del st.query_params[QUERY_SID_KEY]
        return False

    _apply_session(sid, expires_at)
    if st.query_params.get(QUERY_SID_KEY) != sid:
        st.query_params[QUERY_SID_KEY] = sid
    return True


def is_authenticated() -> bool:
    if not st.session_state.get("authenticated"):
        return False
    expires = st.session_state.get("auth_expires_at")
    if expires and datetime.now() > expires:
        logout()
        return False
    sid = st.session_state.get("auth_sid") or _sid_from_query()
    if validate_session(sid) is None:
        logout()
        return False
    return True


def logout() -> None:
    sid = st.session_state.get("auth_sid") or _sid_from_query()
    delete_session(sid)
    for key in ("authenticated", "auth_sid", "auth_expires_at", "login_password"):
        st.session_state.pop(key, None)
    if QUERY_SID_KEY in st.query_params:
        del st.query_params[QUERY_SID_KEY]


def require_login() -> None:
    if not is_authenticated():
        if _restore_session_from_query():
            st.rerun()

    if is_authenticated():
        return

    expected = get_ui_password()
    if not expected:
        st.title("KONA FUTURES")
        st.error(
            "UI 비밀번호가 설정되어 있지 않습니다. "
            "cfg/config.yml에 ui_password를 추가하세요."
        )
        st.stop()

    st.title("KONA FUTURES")
    st.caption("로그인 후 잔고·트레일링을 사용할 수 있습니다.")
    pwd = st.text_input("비밀번호", type="password", key="login_password")
    if st.button("로그인", type="primary", key="login_submit"):
        if hmac.compare_digest(pwd, expected):
            sid = create_session(hours=SESSION_HOURS)
            expires_at = datetime.now() + timedelta(hours=SESSION_HOURS)
            _apply_session(sid, expires_at)
            st.query_params[QUERY_SID_KEY] = sid
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()


def render_logout_button() -> None:
    if st.button("로그아웃", key="logout", width="stretch"):
        logout()
        st.rerun()
