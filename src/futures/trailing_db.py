"""트레일링 스톱 상태 SQLite."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent.parent / "db"
DB_PATH = DB_DIR / "trailing.db"  # legacy; 신규는 db_path_for(profile) 사용

STATUS_STOPPED = "stopped"
STATUS_IDLE = STATUS_STOPPED  # legacy alias
STATUS_WATCHING = "watching"
STATUS_TRIGGERED = "triggered"
STATUS_CLOSING = "closing"
STATUS_CLOSED = "closed"
STATUS_ERROR = "error"

WORKER_STALE_SEC = 90


def db_path_for(profile: str) -> Path:
    return DB_DIR / f"trailing_{profile}.db"


def _migrate_legacy_db(profile: str, target: Path) -> None:
    legacy = DB_PATH
    if target.exists() or not legacy.exists():
        return
    if profile != "mock_futures":
        return
    legacy.rename(target)


def connect(
    db_path: Path | str | None = None,
    *,
    profile: str | None = None,
) -> sqlite3.Connection:
    if db_path is not None:
        path = Path(db_path)
    else:
        from kis_client import get_active_profile

        profile = profile or get_active_profile()
        path = db_path_for(profile)
        _migrate_legacy_db(profile, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trail_positions (
            symbol TEXT NOT NULL PRIMARY KEY,
            prdt_name TEXT,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            trail_points REAL NOT NULL,
            entry_price REAL,
            bar_extreme REAL,
            bar_cursor_datetime TEXT,
            extreme_price REAL,
            stop_price REAL,
            last_price REAL,
            status TEXT NOT NULL,
            last_order_no TEXT,
            order_unverified INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trail_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT,
            event TEXT NOT NULL,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS worker_heartbeat (
            profile TEXT NOT NULL PRIMARY KEY,
            last_seen_at TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS account_summary (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS held_positions (
            symbol TEXT NOT NULL PRIMARY KEY,
            prdt_name TEXT,
            side TEXT NOT NULL,
            side_label TEXT,
            qty REAL,
            lqd_qty REAL,
            entry_price REAL,
            purchase_amt REAL,
            realized_pnl REAL,
            last_price REAL,
            raw_json TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    _ensure_columns(
        conn,
        {
            "entry_price": "REAL",
            "bar_extreme": "REAL",
            "bar_cursor_datetime": "TEXT",
            "order_unverified": "INTEGER DEFAULT 0",
        },
    )
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection, columns: dict[str, str]) -> None:
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(trail_positions)").fetchall()
    }
    for name, col_type in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE trail_positions ADD COLUMN {name} {col_type}"
            )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_event(
    conn: sqlite3.Connection,
    event: str,
    message: str,
    symbol: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO trail_events (created_at, symbol, event, message)
        VALUES (?, ?, ?, ?)
        """,
        (_now(), symbol, event, message),
    )


def list_positions(conn: sqlite3.Connection, include_closed: bool = False) -> list[dict]:
    if include_closed:
        rows = conn.execute(
            "SELECT * FROM trail_positions ORDER BY updated_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM trail_positions
            WHERE status != ?
            ORDER BY updated_at DESC
            """,
            (STATUS_CLOSED,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_position(conn: sqlite3.Connection, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM trail_positions WHERE symbol = ?", (symbol,)
    ).fetchone()
    return dict(row) if row else None


def upsert_position(conn: sqlite3.Connection, data: dict) -> None:
    payload = {
        "symbol": data["symbol"],
        "prdt_name": data.get("prdt_name") or "",
        "side": data["side"],
        "qty": float(data["qty"]),
        "enabled": 1 if data.get("enabled", False) else 0,
        "trail_points": float(data["trail_points"]),
        "entry_price": data.get("entry_price"),
        "bar_extreme": data.get("bar_extreme"),
        "bar_cursor_datetime": data.get("bar_cursor_datetime"),
        "extreme_price": data.get("extreme_price"),
        "stop_price": data.get("stop_price"),
        "last_price": data.get("last_price"),
        "status": data.get("status") or STATUS_STOPPED,
        "last_order_no": data.get("last_order_no") or "",
        "order_unverified": 1 if data.get("order_unverified") else 0,
        "updated_at": data.get("updated_at") or _now(),
    }
    conn.execute(
        """
        INSERT INTO trail_positions (
            symbol, prdt_name, side, qty, enabled, trail_points,
            entry_price, bar_extreme, bar_cursor_datetime,
            extreme_price, stop_price, last_price, status, last_order_no,
            order_unverified, updated_at
        ) VALUES (
            :symbol, :prdt_name, :side, :qty, :enabled, :trail_points,
            :entry_price, :bar_extreme, :bar_cursor_datetime,
            :extreme_price, :stop_price, :last_price, :status, :last_order_no,
            :order_unverified, :updated_at
        )
        ON CONFLICT(symbol) DO UPDATE SET
            prdt_name=excluded.prdt_name,
            side=excluded.side,
            qty=excluded.qty,
            enabled=excluded.enabled,
            trail_points=excluded.trail_points,
            entry_price=excluded.entry_price,
            bar_extreme=excluded.bar_extreme,
            bar_cursor_datetime=excluded.bar_cursor_datetime,
            extreme_price=excluded.extreme_price,
            stop_price=excluded.stop_price,
            last_price=excluded.last_price,
            status=excluded.status,
            last_order_no=excluded.last_order_no,
            order_unverified=excluded.order_unverified,
            updated_at=excluded.updated_at
        """,
        payload,
    )


def _compute_stop(side: str, extreme: float, trail_points: float) -> float:
    if side == "long":
        return extreme - trail_points
    return extreme + trail_points


def start_trailing(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    trail_points: float,
    side: str,
    qty: float,
    prdt_name: str,
    last_price: float,
    entry_price: float | None = None,
    snapshot: dict | None = None,
) -> None:
    entry = entry_price if entry_price is not None else last_price
    payload = {
        "symbol": symbol,
        "prdt_name": prdt_name,
        "side": side,
        "qty": qty,
        "enabled": True,
        "trail_points": trail_points,
        "entry_price": entry,
        "bar_extreme": None,
        "bar_cursor_datetime": None,
        "extreme_price": None,
        "stop_price": None,
        "last_price": last_price,
        "status": STATUS_WATCHING,
        "last_order_no": "",
    }
    if snapshot:
        payload.update(snapshot)
    upsert_position(conn, payload)
    stop = payload.get("stop_price")
    add_event(
        conn,
        "trail_start",
        f"offset={trail_points} entry={entry} px={last_price} stop={stop}",
        symbol=symbol,
    )


def close_position_from_balance(conn: sqlite3.Connection, row: dict) -> None:
    """잔고에서 포지션이 사라졌을 때 (수동 청산 등) 트레일 상태를 정리한다."""
    upsert_position(
        conn,
        {
            **row,
            "qty": 0,
            "enabled": False,
            "status": STATUS_CLOSED,
            "entry_price": None,
            "bar_extreme": None,
            "bar_cursor_datetime": None,
            "extreme_price": None,
            "stop_price": None,
            "last_price": None,
            "last_order_no": "",
            "order_unverified": False,
        },
    )
    add_event(
        conn,
        "position_cleared",
        "잔고에서 포지션 소멸 — 트레일 상태 초기화",
        symbol=row["symbol"],
    )


def stop_trailing(conn: sqlite3.Connection, symbol: str) -> None:
    row = get_position(conn, symbol)
    if not row:
        return
    row["enabled"] = False
    row["status"] = STATUS_STOPPED
    row["updated_at"] = _now()
    upsert_position(conn, row)
    add_event(conn, "trail_stop", "trailing disabled", symbol=symbol)


def update_strategy(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    trail_points: float | None = None,
    enabled: bool | None = None,
) -> None:
    row = get_position(conn, symbol)
    if not row:
        raise KeyError(f"트레일 포지션 없음: {symbol}")
    if trail_points is not None:
        row["trail_points"] = float(trail_points)
        extreme = row.get("extreme_price")
        if extreme is not None:
            if row["side"] == "long":
                row["stop_price"] = float(extreme) - float(trail_points)
            else:
                row["stop_price"] = float(extreme) + float(trail_points)
    if enabled is not None:
        row["enabled"] = enabled
        if enabled:
            if row.get("status") in (STATUS_ERROR, STATUS_STOPPED, STATUS_CLOSED, "idle"):
                row["status"] = STATUS_WATCHING
        else:
            row["status"] = STATUS_STOPPED
    row["updated_at"] = _now()
    upsert_position(conn, row)
    add_event(
        conn,
        "strategy_update",
        f"trail_points={row['trail_points']}, enabled={bool(row['enabled'])}",
        symbol=symbol,
    )


def save_account_summary(conn: sqlite3.Connection, summary: dict) -> None:
    conn.execute(
        """
        INSERT INTO account_summary (id, payload, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """,
        (json.dumps(summary or {}, ensure_ascii=False), _now()),
    )


def get_account_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT payload FROM account_summary WHERE id = 1"
    ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _held_from_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = {}
    raw_json = data.get("raw_json") or ""
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            raw = {}
    data["raw"] = raw
    return data


def list_held_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM held_positions
        ORDER BY prdt_name, symbol
        """
    ).fetchall()
    return [_held_from_row(row) for row in rows]


def upsert_held_position(conn: sqlite3.Connection, data: dict) -> None:
    raw_json = data.get("raw_json")
    if raw_json is None:
        raw_json = json.dumps(data.get("raw") or {}, ensure_ascii=False)
    payload = {
        "symbol": data["symbol"],
        "prdt_name": data.get("prdt_name") or "",
        "side": data["side"],
        "side_label": data.get("side_label") or "",
        "qty": data.get("qty"),
        "lqd_qty": data.get("lqd_qty"),
        "entry_price": data.get("entry_price"),
        "purchase_amt": data.get("purchase_amt"),
        "realized_pnl": data.get("realized_pnl"),
        "last_price": data.get("last_price"),
        "raw_json": raw_json,
        "updated_at": data.get("updated_at") or _now(),
    }
    conn.execute(
        """
        INSERT INTO held_positions (
            symbol, prdt_name, side, side_label, qty, lqd_qty,
            entry_price, purchase_amt, realized_pnl, last_price, raw_json, updated_at
        ) VALUES (
            :symbol, :prdt_name, :side, :side_label, :qty, :lqd_qty,
            :entry_price, :purchase_amt, :realized_pnl, :last_price, :raw_json, :updated_at
        )
        ON CONFLICT(symbol) DO UPDATE SET
            prdt_name=excluded.prdt_name,
            side=excluded.side,
            side_label=excluded.side_label,
            qty=excluded.qty,
            lqd_qty=excluded.lqd_qty,
            entry_price=excluded.entry_price,
            purchase_amt=excluded.purchase_amt,
            realized_pnl=excluded.realized_pnl,
            last_price=COALESCE(held_positions.last_price, excluded.last_price),
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        payload,
    )


def replace_held_positions(conn: sqlite3.Connection, rows: list[dict]) -> None:
    symbols = [str(row["symbol"]) for row in rows]
    for row in rows:
        upsert_held_position(conn, row)
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        conn.execute(
            f"DELETE FROM held_positions WHERE symbol NOT IN ({placeholders})",
            symbols,
        )
    else:
        conn.execute("DELETE FROM held_positions")


def update_held_last_price(
    conn: sqlite3.Connection,
    symbol: str,
    last_price: float,
) -> None:
    conn.execute(
        """
        UPDATE held_positions
        SET last_price = ?, updated_at = ?
        WHERE symbol = ?
        """,
        (last_price, _now(), symbol),
    )


def list_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM trail_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def touch_worker_heartbeat(
    conn: sqlite3.Connection,
    profile: str,
    *,
    dry_run: bool = False,
    seen_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO worker_heartbeat (profile, last_seen_at, dry_run)
        VALUES (?, ?, ?)
        ON CONFLICT(profile) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            dry_run=excluded.dry_run
        """,
        (profile, seen_at or _now(), 1 if dry_run else 0),
    )


def get_worker_heartbeat(conn: sqlite3.Connection, profile: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM worker_heartbeat WHERE profile = ?", (profile,)
    ).fetchone()
    return dict(row) if row else None


def worker_is_online(
    conn: sqlite3.Connection,
    profile: str,
    *,
    max_age_sec: float = WORKER_STALE_SEC,
) -> bool:
    row = get_worker_heartbeat(conn, profile)
    if not row:
        return False
    try:
        seen = datetime.strptime(row["last_seen_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - seen).total_seconds() <= max_age_sec
