"""선물 SQLite 스키마 및 월물별 저장."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from kis_client import issue_access_token, load_profile

from .futures_contracts import (
    FuturesContract,
    contract_end_date,
    contract_end_datetime,
    contract_month_from_symbol,
    contract_sync_order,
    discover_contracts,
    list_board_contracts,
    resolve_front_contract,
)
from .futures_daily import CHUNK_DAYS, download_daily_for_symbol, _date_add
from .futures_minute import (
    completed_cutoff,
    download_minute_for_symbol,
    last_possible_completed_bar,
    session_open_datetime,
)
from .futures_products import (
    DAILY_START_DATE,
    DEFAULT_SESSIONS,
    PRODUCTS,
    product_key_for_symbol,
)

DB_DIR = Path(__file__).resolve().parent.parent.parent / "db"
DB_PATH = DB_DIR / "futures.db"

# (product, symbol, session) → 마지막으로 확인한 완결시각. 워커 프로세스 내 중복 API 방지.
_probed_until: dict[tuple[str, str, str], str] = {}

LEGACY_TABLES = (
    "kospi200_1m",
    "kospi200_1d",
    "kosdaq150_1m",
    "kosdaq150_1d",
)

MINUTE_COLUMNS = (
    "product",
    "symbol",
    "session",
    "contract_month",
    "trade_datetime",
    "trade_date",
    "trade_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

DAILY_COLUMNS = (
    "product",
    "symbol",
    "session",
    "contract_month",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def reset_db(conn: sqlite3.Connection) -> None:
    for table in LEGACY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("DROP TABLE IF EXISTS minute_bars")
    conn.execute("DROP TABLE IF EXISTS daily_bars")
    conn.execute("DROP TABLE IF EXISTS sync_meta")
    conn.commit()
    init_db(conn)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS minute_bars (
            product         TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            session         TEXT NOT NULL DEFAULT 'day',
            contract_month  TEXT NOT NULL,
            trade_datetime  TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            trade_time      TEXT NOT NULL,
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL,
            volume          INTEGER,
            PRIMARY KEY (product, symbol, session, trade_datetime)
        );

        CREATE INDEX IF NOT EXISTS idx_minute_bars_product_dt
            ON minute_bars (product, session, trade_datetime);
        CREATE INDEX IF NOT EXISTS idx_minute_bars_contract_dt
            ON minute_bars (contract_month, session, trade_datetime);

        CREATE TABLE IF NOT EXISTS daily_bars (
            product         TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            session         TEXT NOT NULL DEFAULT 'day',
            contract_month  TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL,
            volume          INTEGER,
            PRIMARY KEY (product, symbol, session, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_bars_product_date
            ON daily_bars (product, session, trade_date);

        CREATE TABLE IF NOT EXISTS sync_meta (
            product         TEXT NOT NULL,
            interval        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            session         TEXT NOT NULL DEFAULT 'day',
            contract_month  TEXT,
            last_key        TEXT,
            updated_at      TEXT NOT NULL,
            PRIMARY KEY (product, interval, symbol, session)
        );
        """
    )
    conn.commit()


def get_symbol_datetime_bounds(
    conn: sqlite3.Connection,
    product_key: str,
    symbol: str,
    session: str,
) -> tuple[str | None, str | None]:
    row = conn.execute(
        """
        SELECT MIN(trade_datetime), MAX(trade_datetime)
        FROM minute_bars
        WHERE product = ? AND symbol = ? AND session = ?
        """,
        (product_key, symbol, session),
    ).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1]


def get_latest_minute_bar(
    conn: sqlite3.Connection,
    product_key: str,
    symbol: str,
    session: str | None = None,
    *,
    until_datetime: str | None = None,
) -> dict | None:
    sql = """
        SELECT trade_datetime, close
        FROM minute_bars
        WHERE product = ? AND symbol = ?
    """
    params: list = [product_key, symbol]
    if session:
        sql += " AND session = ?"
        params.append(session)
    if until_datetime:
        sql += " AND trade_datetime <= ?"
        params.append(until_datetime)
    sql += " ORDER BY trade_datetime DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return {"trade_datetime": row[0], "close": row[1]}


def list_minute_bars_after(
    conn: sqlite3.Connection,
    product_key: str,
    symbol: str,
    session: str | None = None,
    *,
    after_datetime: str,
    until_datetime: str | None = None,
) -> list[dict]:
    sql = """
        SELECT trade_datetime, close
        FROM minute_bars
        WHERE product = ? AND symbol = ?
          AND trade_datetime > ?
    """
    params: list = [product_key, symbol, after_datetime]
    if session:
        sql += " AND session = ?"
        params.append(session)
    if until_datetime:
        sql += " AND trade_datetime <= ?"
        params.append(until_datetime)
    sql += " ORDER BY trade_datetime"
    return [
        {"trade_datetime": row[0], "close": row[1]}
        for row in conn.execute(sql, params)
    ]


def get_symbol_date_bounds(
    conn: sqlite3.Connection,
    product_key: str,
    symbol: str,
    session: str,
) -> tuple[str | None, str | None]:
    row = conn.execute(
        """
        SELECT MIN(trade_date), MAX(trade_date)
        FROM daily_bars
        WHERE product = ? AND symbol = ? AND session = ?
        """,
        (product_key, symbol, session),
    ).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1]


def upsert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    pk_cols: tuple[str, ...],
    rows: list[dict],
) -> int:
    if not rows:
        return 0

    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{col}=excluded.{col}" for col in columns if col not in pk_cols
    )
    pk = ", ".join(pk_cols)
    sql = f"""
        INSERT INTO {table} ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT({pk}) DO UPDATE SET {updates}
    """
    values = [tuple(row[col] for col in columns) for row in rows]
    conn.executemany(sql, values)
    conn.commit()
    return len(values)


def update_meta(
    conn: sqlite3.Connection,
    product_key: str,
    interval: str,
    symbol: str,
    session: str,
    contract_month: str,
    last_key: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_meta (product, interval, symbol, session, contract_month, last_key, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product, interval, symbol, session) DO UPDATE SET
            contract_month=excluded.contract_month,
            last_key=excluded.last_key,
            updated_at=excluded.updated_at
        """,
        (
            product_key,
            interval,
            symbol,
            session,
            contract_month,
            last_key,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def _attach_contract_fields(
    product_key: str,
    contract: FuturesContract,
    bars: list[dict],
    *,
    session: str,
) -> list[dict]:
    enriched: list[dict] = []
    for bar in bars:
        enriched.append(
            {
                "product": product_key,
                "symbol": contract.symbol,
                "session": session,
                "contract_month": contract.contract_month,
                **bar,
            }
        )
    return enriched


def sync_minute_symbol(
    conn: sqlite3.Connection,
    profile: str,
    product_key: str,
    contract: FuturesContract,
    *,
    session: str,
    end_dt: datetime | None = None,
    extend: bool = False,
    verbose: bool = False,
    token: str | None = None,
) -> tuple[int, str | None, str | None, str | None, str | None]:
    first_dt, last_dt = get_symbol_datetime_bounds(
        conn, product_key, contract.symbol, session
    )
    original_first_dt = first_dt
    session_end = last_possible_completed_bar(session)
    contract_end = contract_end_datetime(contract.contract_month, session)
    end_dt = min(session_end, contract_end, completed_cutoff())

    cfg = load_profile(profile)
    if token is None:
        token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])[
            "access_token"
        ]

    all_bars: dict[str, dict] = {}
    session_label = "야간" if session == "night" else "주간"

    if verbose and extend and not last_dt:
        pass
    elif verbose:
        print(
            f"  → [{session_label}] {contract.symbol} ({contract.contract_month}) "
            f"{'최근 갱신' if not extend or last_dt else '과거 확장'}",
            flush=True,
        )
    if last_dt:
        _, recent = download_minute_for_symbol(
            profile,
            product_key,
            contract.symbol,
            end_dt,
            until_datetime=last_dt,
            token=token,
            verbose=verbose,
            session=session,
        )
        all_bars.update({bar["trade_datetime"]: bar for bar in recent})
        if extend and original_first_dt:
            if verbose:
                print(
                    f"  → [{session_label}] {contract.symbol} 과거 확장 (기준 {original_first_dt})",
                    flush=True,
                )
            end_backfill = datetime.strptime(
                original_first_dt, "%Y%m%d%H%M%S"
            ) - timedelta(minutes=1)
            _, older = download_minute_for_symbol(
                profile,
                product_key,
                contract.symbol,
                end_backfill,
                until_datetime=None,
                token=token,
                paginate=True,
                verbose=verbose,
                stop_on_empty=True,
                session=session,
            )
            all_bars.update({bar["trade_datetime"]: bar for bar in older})
    elif extend:
        if verbose:
            print(
                f"  → [{session_label}] {contract.symbol} ({contract.contract_month}) 과거 확장",
                flush=True,
            )
        _, recent = download_minute_for_symbol(
            profile,
            product_key,
            contract.symbol,
            end_dt,
            until_datetime=None,
            token=token,
            paginate=True,
            verbose=verbose,
            stop_on_empty=True,
            session=session,
        )
        all_bars.update({bar["trade_datetime"]: bar for bar in recent})
    else:
        _, recent = download_minute_for_symbol(
            profile,
            product_key,
            contract.symbol,
            end_dt,
            until_datetime=None,
            token=token,
            paginate=False,
            verbose=verbose,
            session=session,
        )
        all_bars.update({bar["trade_datetime"]: bar for bar in recent})

    cutoff_key = completed_cutoff().strftime("%Y%m%d%H%M%S")
    bars = _attach_contract_fields(
        product_key,
        contract,
        [
            all_bars[key]
            for key in sorted(all_bars)
            if key <= cutoff_key
        ],
        session=session,
    )
    saved = upsert_rows(
        conn,
        "minute_bars",
        MINUTE_COLUMNS,
        ("product", "symbol", "session", "trade_datetime"),
        bars,
    )
    new_first, new_last = get_symbol_datetime_bounds(
        conn, product_key, contract.symbol, session
    )
    update_meta(
        conn,
        product_key,
        "1m",
        contract.symbol,
        session,
        contract.contract_month,
        new_last,
    )
    return saved, new_first, new_last, first_dt, last_dt


def sync_minute_if_needed(
    conn: sqlite3.Connection,
    profile: str,
    symbol: str,
    session: str,
    *,
    token: str | None = None,
    now: datetime | None = None,
    fill: bool = False,
) -> dict:
    """워커용 증분 분봉 동기화. 새 완결 봉이 생길 수 있을 때만 API 호출.

    fill=True 이면 프로세스 내 probed 캐시를 무시하고, DB가 비어 있어도
    해당 세션 개장 시각까지 페이지한다 (세션 마감 catch-up).
    """
    now = now or datetime.now()
    product_key = product_key_for_symbol(symbol)
    if not product_key:
        return {"symbol": symbol, "skipped": True, "reason": "unknown_product", "saved": 0}

    contract_month = contract_month_from_symbol(symbol)
    if not contract_month:
        return {"symbol": symbol, "skipped": True, "reason": "unknown_month", "saved": 0}

    last_possible = last_possible_completed_bar(session, now)
    last_possible_key = last_possible.strftime("%Y%m%d%H%M%S")
    cache_key = (product_key, symbol, session)
    if not fill:
        probed = _probed_until.get(cache_key, "")
        if probed >= last_possible_key:
            return {
                "symbol": symbol,
                "skipped": True,
                "reason": "probed",
                "saved": 0,
                "until": last_possible_key,
            }

    _, last_dt = get_symbol_datetime_bounds(conn, product_key, symbol, session)
    if last_dt and last_dt >= last_possible_key:
        _probed_until[cache_key] = last_possible_key
        return {
            "symbol": symbol,
            "skipped": True,
            "reason": "db_current",
            "saved": 0,
            "last_dt": last_dt,
            "until": last_possible_key,
        }

    contract = FuturesContract(symbol=symbol, contract_month=contract_month)
    end_dt = min(last_possible, contract_end_datetime(contract_month, session))
    if fill:
        paginate = True
        until_datetime = last_dt or session_open_datetime(session, now).strftime(
            "%Y%m%d%H%M%S"
        )
    else:
        paginate = last_dt is not None
        until_datetime = last_dt
    _, bars = download_minute_for_symbol(
        profile,
        product_key,
        symbol,
        end_dt,
        until_datetime=until_datetime,
        token=token,
        paginate=paginate,
        session=session,
    )
    new_bars = [
        bar
        for bar in bars
        if (not last_dt or bar["trade_datetime"] > last_dt)
        and bar["trade_datetime"] <= last_possible_key
    ]
    rows = _attach_contract_fields(product_key, contract, new_bars, session=session)
    saved = upsert_rows(
        conn,
        "minute_bars",
        MINUTE_COLUMNS,
        ("product", "symbol", "session", "trade_datetime"),
        rows,
    )
    _, new_last = get_symbol_datetime_bounds(conn, product_key, symbol, session)
    update_meta(
        conn,
        product_key,
        "1m",
        symbol,
        session,
        contract_month,
        new_last,
    )
    if saved == 0:
        _probed_until[cache_key] = last_possible_key
        reason = "no_new_bars"
    else:
        _probed_until[cache_key] = last_possible_key
        reason = "fetched"
    return {
        "symbol": symbol,
        "skipped": False,
        "reason": reason,
        "saved": saved,
        "last_dt": new_last,
        "until": last_possible_key,
    }


def sync_minute_symbols_if_needed(
    conn: sqlite3.Connection,
    profile: str,
    symbols: list[str] | set[str],
    session: str | None = None,
    *,
    token: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """감시 종목의 주간·야간 minute_bars를 필요할 때만 증분 갱신."""
    sessions = (session,) if session else DEFAULT_SESSIONS
    results = []
    for symbol in sorted(set(symbols)):
        for sess in sessions:
            results.append(
                sync_minute_if_needed(
                    conn,
                    profile,
                    symbol,
                    sess,
                    token=token,
                    now=now,
                )
            )
    return results


def catchup_listed_session(
    conn: sqlite3.Connection,
    profile: str,
    session: str,
    *,
    token: str | None = None,
    now: datetime | None = None,
    product_keys: tuple[str, ...] | None = None,
) -> dict:
    """전광판 상장 월물만 해당 세션 분봉·일봉을 DB 마지막까지 채운다."""
    now = now or datetime.now()
    cfg = load_profile(profile)
    if token is None:
        token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])[
            "access_token"
        ]
    keys = product_keys or tuple(PRODUCTS)
    session_label = "야간" if session == "night" else "주간"
    items: list[dict] = []
    errors: list[dict] = []
    print(f"catchup {session_label} 시작 {now:%Y-%m-%d %H:%M:%S}", flush=True)

    for product_key in keys:
        try:
            contracts = list_board_contracts(
                profile,
                token,
                cfg["appkey"],
                cfg["seckey"],
                product_key,
            )
        except Exception as exc:
            errors.append({"product": product_key, "error": str(exc)})
            print(f"catchup {session_label} {product_key} 전광판 실패: {exc}", flush=True)
            continue
        label = PRODUCTS[product_key]["label"]
        print(
            f"catchup {session_label} {product_key} ({label}) 월물 {len(contracts)}개",
            flush=True,
        )
        for contract in contracts:
            entry: dict = {
                "product": product_key,
                "symbol": contract.symbol,
                "contract_month": contract.contract_month,
            }
            try:
                minute = sync_minute_if_needed(
                    conn,
                    profile,
                    contract.symbol,
                    session,
                    token=token,
                    now=now,
                    fill=True,
                )
                daily_saved, _, daily_last, _, _ = sync_daily_symbol(
                    conn,
                    profile,
                    product_key,
                    contract,
                    session=session,
                    extend=False,
                    token=token,
                )
                entry["minute"] = {
                    "saved": int(minute.get("saved") or 0),
                    "reason": minute.get("reason"),
                    "skipped": bool(minute.get("skipped")),
                    "last_dt": minute.get("last_dt"),
                }
                entry["daily"] = {"saved": daily_saved, "last_date": daily_last}
                print(
                    f"  {contract.symbol} ({contract.contract_month}) "
                    f"1m+{entry['minute']['saved']} 1d+{daily_saved}",
                    flush=True,
                )
            except Exception as exc:
                entry["error"] = str(exc)
                errors.append(
                    {
                        "product": product_key,
                        "symbol": contract.symbol,
                        "error": str(exc),
                    }
                )
                print(
                    f"  {contract.symbol} ({contract.contract_month}) 실패: {exc}",
                    flush=True,
                )
            items.append(entry)

    minute_saved = sum(
        int((item.get("minute") or {}).get("saved") or 0) for item in items
    )
    daily_saved = sum(int((item.get("daily") or {}).get("saved") or 0) for item in items)
    print(
        f"catchup {session_label} 완료 symbols={len(items)} "
        f"1m+{minute_saved} 1d+{daily_saved} errors={len(errors)}",
        flush=True,
    )
    return {
        "session": session,
        "symbols": len(items),
        "minute_saved": minute_saved,
        "daily_saved": daily_saved,
        "errors": errors,
        "ok": not errors and bool(items),
        "items": [
            {
                "symbol": item["symbol"],
                "month": item.get("contract_month"),
                "minute_saved": int((item.get("minute") or {}).get("saved") or 0),
                "daily_saved": int((item.get("daily") or {}).get("saved") or 0),
                "error": item.get("error"),
            }
            for item in items
        ],
    }


def sync_daily_symbol(
    conn: sqlite3.Connection,
    profile: str,
    product_key: str,
    contract: FuturesContract,
    *,
    session: str,
    extend: bool = False,
    verbose: bool = False,
    token: str | None = None,
) -> tuple[int, str | None, str | None, str | None, str | None]:
    first_date, last_date = get_symbol_date_bounds(
        conn, product_key, contract.symbol, session
    )
    original_first_date = first_date
    today = datetime.now().strftime("%Y%m%d")
    end_date = min(today, contract_end_date(contract.contract_month, session))

    cfg = load_profile(profile)
    if token is None:
        token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])[
            "access_token"
        ]

    all_bars: dict[str, dict] = {}
    session_label = "야간" if session == "night" else "주간"

    if last_date:
        if verbose:
            print(
                f"  → [{session_label}] {contract.symbol} ({contract.contract_month}) 최근 갱신",
                flush=True,
            )
        _, recent = download_daily_for_symbol(
            profile,
            product_key,
            contract.symbol,
            last_date,
            end_date,
            token=token,
            verbose=verbose,
            session=session,
        )
        all_bars.update({bar["trade_date"]: bar for bar in recent})
        if extend and original_first_date and original_first_date > DAILY_START_DATE:
            end_backfill = (
                datetime.strptime(original_first_date, "%Y%m%d") - timedelta(days=1)
            ).strftime("%Y%m%d")
            if verbose:
                print(
                    f"  → [{session_label}] {contract.symbol} 과거 확장 "
                    f"({DAILY_START_DATE}~{end_backfill})",
                    flush=True,
                )
            _, older = download_daily_for_symbol(
                profile,
                product_key,
                contract.symbol,
                DAILY_START_DATE,
                end_backfill,
                token=token,
                verbose=verbose,
                chunk_backward=True,
                stop_on_empty=True,
                session=session,
            )
            all_bars.update({bar["trade_date"]: bar for bar in older})
    elif extend:
        if verbose:
            print(
                f"  → [{session_label}] {contract.symbol} ({contract.contract_month}) 과거 확장",
                flush=True,
            )
        _, recent = download_daily_for_symbol(
            profile,
            product_key,
            contract.symbol,
            DAILY_START_DATE,
            end_date,
            token=token,
            verbose=verbose,
            chunk_backward=True,
            stop_on_empty=True,
            session=session,
        )
        all_bars.update({bar["trade_date"]: bar for bar in recent})
    else:
        if verbose:
            print(
                f"  → [{session_label}] {contract.symbol} ({contract.contract_month}) 최근 갱신",
                flush=True,
            )
        start = max(DAILY_START_DATE, _date_add(end_date, -(CHUNK_DAYS - 1)))
        _, recent = download_daily_for_symbol(
            profile,
            product_key,
            contract.symbol,
            start,
            end_date,
            token=token,
            verbose=verbose,
            stop_on_empty=True,
            session=session,
        )
        all_bars.update({bar["trade_date"]: bar for bar in recent})

    bars = _attach_contract_fields(
        product_key,
        contract,
        [all_bars[key] for key in sorted(all_bars)],
        session=session,
    )
    saved = upsert_rows(
        conn,
        "daily_bars",
        DAILY_COLUMNS,
        ("product", "symbol", "session", "trade_date"),
        bars,
    )
    new_first, new_last = get_symbol_date_bounds(
        conn, product_key, contract.symbol, session
    )
    update_meta(
        conn,
        product_key,
        "1d",
        contract.symbol,
        session,
        contract.contract_month,
        new_last,
    )
    return saved, new_first, new_last, first_date, last_date


def sync_product_contracts(
    conn: sqlite3.Connection,
    profile: str,
    product_key: str,
    *,
    interval: str,
    extend: bool = False,
    include_past: bool = True,
    sessions: tuple[str, ...] = DEFAULT_SESSIONS,
    verbose: bool = False,
) -> list[dict]:
    cfg = load_profile(profile)
    token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])[
        "access_token"
    ]
    contracts = discover_contracts(
        profile,
        token,
        cfg["appkey"],
        cfg["seckey"],
        product_key,
        include_past=include_past,
    )
    front = resolve_front_contract(
        profile, token, cfg["appkey"], cfg["seckey"], product_key, contracts
    )
    ordered = contract_sync_order(contracts, front.contract_month)

    if verbose:
        order_text = ", ".join(
            f"{item.symbol}({item.contract_month})" for item in ordered
        )
        print(f"  월물 순서: {order_text}", flush=True)

    results: list[dict] = []
    for contract in ordered:
        entry = {
            "symbol": contract.symbol,
            "contract_month": contract.contract_month,
            "name": contract.name,
        }
        try:
            for session in sessions:
                if interval in ("1m", "both"):
                    saved, new_first, new_last, old_first, old_last = (
                        sync_minute_symbol(
                            conn,
                            profile,
                            product_key,
                            contract,
                            session=session,
                            extend=extend,
                            verbose=verbose,
                            token=token,
                        )
                    )
                    entry.setdefault("minute", {})[session] = {
                        "saved": saved,
                        "before": (old_first, old_last),
                        "after": (new_first, new_last),
                    }
                if interval in ("1d", "both"):
                    saved, new_first, new_last, old_first, old_last = (
                        sync_daily_symbol(
                            conn,
                            profile,
                            product_key,
                            contract,
                            session=session,
                            extend=extend,
                            verbose=verbose,
                            token=token,
                        )
                    )
                    entry.setdefault("daily", {})[session] = {
                        "saved": saved,
                        "before": (old_first, old_last),
                        "after": (new_first, new_last),
                    }
        except Exception as exc:
            entry["error"] = str(exc)
        results.append(entry)
    return results
