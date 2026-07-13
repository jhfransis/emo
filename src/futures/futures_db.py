"""선물 SQLite 스키마 및 저장."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from kis_client import issue_access_token, load_profile

from .futures_daily import CHUNK_DAYS, download_daily_range_data, _date_add
from .futures_minute import download_minute_backward
from .futures_products import DAILY_START_DATE, PRODUCTS

DB_DIR = Path(__file__).resolve().parent.parent.parent / "db"
DB_PATH = DB_DIR / "futures.db"

MINUTE_COLUMNS = (
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
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_meta (
            product     TEXT NOT NULL,
            interval    TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            symbol      TEXT,
            last_key    TEXT,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (product, interval)
        );
        """
    )
    for product in PRODUCTS.values():
        conn.executescript(_create_minute_table_sql(product["table_1m"]))
        conn.executescript(_create_daily_table_sql(product["table_1d"]))
    conn.commit()


def _create_minute_table_sql(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        trade_datetime TEXT NOT NULL PRIMARY KEY,
        trade_date     TEXT NOT NULL,
        trade_time     TEXT NOT NULL,
        open           REAL,
        high           REAL,
        low            REAL,
        close          REAL,
        volume         INTEGER
    );
    """


def _create_daily_table_sql(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        trade_date   TEXT NOT NULL PRIMARY KEY,
        open         REAL,
        high         REAL,
        low          REAL,
        close        REAL,
        volume       INTEGER
    );
    """


def get_datetime_bounds(
    conn: sqlite3.Connection, table: str
) -> tuple[str | None, str | None]:
    row = conn.execute(
        f"SELECT MIN(trade_datetime), MAX(trade_datetime) FROM {table}"
    ).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1]


def get_date_bounds(conn: sqlite3.Connection, table: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        f"SELECT MIN(trade_date), MAX(trade_date) FROM {table}"
    ).fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], row[1]


def upsert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    pk: str,
    rows: list[dict],
) -> int:
    if not rows:
        return 0

    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != pk)
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
    table: str,
    symbol: str,
    last_key: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_meta (product, interval, table_name, symbol, last_key, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(product, interval) DO UPDATE SET
            table_name=excluded.table_name,
            symbol=excluded.symbol,
            last_key=excluded.last_key,
            updated_at=excluded.updated_at
        """,
        (
            product_key,
            interval,
            table,
            symbol,
            last_key,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def sync_minute_product(
    conn: sqlite3.Connection,
    profile: str,
    product_key: str,
    mode: str,
    end_dt: datetime | None = None,
    extend: bool = False,
    verbose: bool = False,
) -> tuple[str, int, str | None, str | None, str | None, str | None]:
    product = PRODUCTS[product_key]
    table = product["table_1m"]
    first_dt, last_dt = get_datetime_bounds(conn, table)
    original_first_dt = first_dt
    end_dt = end_dt or datetime.now()

    cfg = load_profile(profile)
    token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])["access_token"]

    all_bars: dict[str, dict] = {}
    symbol: str | None = None

    # 1) 현재 → DB 마지막(포함) 갱신 — 마지막 봉은 미완성일 수 있어 재수집
    if verbose:
        print("  → 최근 갱신", flush=True)
    if last_dt:
        symbol, recent = download_minute_backward(
            profile,
            product_key,
            mode,
            end_dt,
            until_datetime=last_dt,
            token=token,
            verbose=verbose,
        )
        all_bars.update({bar["trade_datetime"]: bar for bar in recent})
    else:
        symbol, recent = download_minute_backward(
            profile,
            product_key,
            mode,
            end_dt,
            until_datetime=None,
            token=token,
            paginate=extend,
            verbose=verbose,
        )
        all_bars.update({bar["trade_datetime"]: bar for bar in recent})

    # 2) --extend: DB 첫 시각 이전 과거 확장 (서버가 없을 때까지)
    if extend:
        anchor_dt = original_first_dt or (min(all_bars.keys()) if all_bars else None)
        if anchor_dt:
            if verbose:
                print(f"  → 과거 확장 (기준 {anchor_dt})", flush=True)
            end_backfill = datetime.strptime(anchor_dt, "%Y%m%d%H%M%S") - timedelta(
                minutes=1
            )
            symbol, older = download_minute_backward(
                profile,
                product_key,
                mode,
                end_backfill,
                until_datetime=None,
                token=token,
                symbol=symbol,
                verbose=verbose,
                stop_on_empty=True,
            )
            all_bars.update({bar["trade_datetime"]: bar for bar in older})

    bars = [all_bars[key] for key in sorted(all_bars)]
    saved = upsert_rows(conn, table, MINUTE_COLUMNS, "trade_datetime", bars)

    new_first, new_last = get_datetime_bounds(conn, table)
    update_meta(conn, product_key, "1m", table, symbol or "", new_last)
    return symbol or "", saved, new_first, new_last, first_dt, last_dt


def sync_daily_product(
    conn: sqlite3.Connection,
    profile: str,
    product_key: str,
    mode: str,
    extend: bool = False,
    verbose: bool = False,
) -> tuple[str, int, str | None, str | None, str | None, str | None]:
    product = PRODUCTS[product_key]
    table = product["table_1d"]
    first_date, last_date = get_date_bounds(conn, table)
    original_first_date = first_date
    today = datetime.now().strftime("%Y%m%d")

    cfg = load_profile(profile)
    token = issue_access_token(profile, cfg["appkey"], cfg["seckey"])["access_token"]

    all_bars: dict[str, dict] = {}
    symbol: str | None = None

    # 1) 오늘 → DB 마지막(포함) 갱신 — 마지막 일봉은 당일 미확정일 수 있어 재수집
    if verbose:
        print("  → 최근 갱신", flush=True)
    if last_date:
        symbol, recent = download_daily_range_data(
            profile,
            product_key,
            mode,
            last_date,
            today,
            token=token,
            verbose=verbose,
        )
        all_bars.update({bar["trade_date"]: bar for bar in recent})
    else:
        if extend:
            start = DAILY_START_DATE
        else:
            start = max(DAILY_START_DATE, _date_add(today, -(CHUNK_DAYS - 1)))
        symbol, recent = download_daily_range_data(
            profile,
            product_key,
            mode,
            start,
            today,
            token=token,
            verbose=verbose,
        )
        all_bars.update({bar["trade_date"]: bar for bar in recent})

    # 2) --extend: DB 첫 날짜 이전 → 30% 시행일까지
    if extend:
        anchor_date = original_first_date or (min(all_bars.keys()) if all_bars else None)
        if anchor_date and anchor_date > DAILY_START_DATE:
            end_backfill = (
                datetime.strptime(anchor_date, "%Y%m%d") - timedelta(days=1)
            ).strftime("%Y%m%d")
            if verbose:
                print(
                    f"  → 과거 확장 ({DAILY_START_DATE}~{end_backfill})",
                    flush=True,
                )
            symbol, older = download_daily_range_data(
                profile,
                product_key,
                mode,
                DAILY_START_DATE,
                end_backfill,
                token=token,
                symbol=symbol,
                verbose=verbose,
                chunk_backward=True,
                stop_on_empty=True,
            )
            all_bars.update({bar["trade_date"]: bar for bar in older})

    bars = [all_bars[key] for key in sorted(all_bars)]
    saved = upsert_rows(conn, table, DAILY_COLUMNS, "trade_date", bars)

    new_first, new_last = get_date_bounds(conn, table)
    update_meta(conn, product_key, "1d", table, symbol or "", new_last)
    return symbol or "", saved, new_first, new_last, first_date, last_date
