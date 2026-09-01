"""선물 1분봉/일봉 SQLite 갱신 (월물별)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from futures.futures_db import (
    DB_PATH,
    init_db,
    reset_db,
    sync_product_contracts,
)
from futures.futures_products import (
    DAILY_START_DATE,
    DEFAULT_SESSIONS,
    PRODUCTS,
    SESSION_DAY,
    SESSION_NIGHT,
)

DEFAULT_PRODUCTS = "kospi200_mini,kospi200,kosdaq150"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="선물 월물별 1분봉/일봉 SQLite 갱신 (연결선물 미사용)",
    )
    parser.add_argument("--profile", default="mock_futures")
    parser.add_argument(
        "--interval",
        choices=("1m", "1d", "both"),
        default="both",
        help="갱신 주기 (기본: both)",
    )
    parser.add_argument(
        "--extend",
        action="store_true",
        help="최근 갱신 후 과거 구간 확장 (1m: API 한도까지, 1d: 20150615까지)",
    )
    parser.add_argument(
        "--no-past",
        action="store_true",
        help="전광판에 없는 과거 만기 월물 코드 추정·수집 생략",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="기존 테이블·데이터 삭제 후 스키마 재생성",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="API 페이지/청크별 진행 상황 출력",
    )
    parser.add_argument(
        "--products",
        default=DEFAULT_PRODUCTS,
        help=f"대상 상품 (기본: {DEFAULT_PRODUCTS})",
    )
    parser.add_argument(
        "--sessions",
        default="day,night",
        help="수집 세션 (day=주간/F, night=야간/CM, 기본: day,night)",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="SQLite DB 경로 (기본: db/futures.db)",
    )
    return parser.parse_args()


def _parse_sessions(raw: str) -> tuple[str, ...]:
    allowed = {SESSION_DAY, SESSION_NIGHT}
    sessions = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip())
    )
    unknown = [item for item in sessions if item not in allowed]
    if unknown:
        raise ValueError(f"알 수 없는 session: {unknown} (day, night)")
    if not sessions:
        raise ValueError("session이 비어 있습니다.")
    return sessions


def _print_interval_result(label: str, data: dict | None) -> None:
    if not data:
        return
    if "day" in data or "night" in data:
        for session in ("day", "night"):
            if session in data:
                session_label = "주간" if session == "day" else "야간"
                _print_interval_result(f"{label}/{session_label}", data[session])
        return
    before_first, before_last = data.get("before") or (None, None)
    after_first, after_last = data.get("after") or (None, None)
    print(f"    [{label}] 저장 {data.get('saved', 0)}건")
    if before_first or before_last:
        print(f"      DB(전): {before_first or '-'} ~ {before_last or '-'}")
    else:
        print("      DB(전): (비어 있음)")
    if after_first or after_last:
        print(f"      DB(후): {after_first or '-'} ~ {after_last or '-'}")
    else:
        print("      DB(후): (변화 없음)")


def main() -> int:
    args = parse_args()
    try:
        sessions = _parse_sessions(args.sessions)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    product_keys = [key.strip() for key in args.products.split(",") if key.strip()]
    unknown = [key for key in product_keys if key not in PRODUCTS]
    if unknown:
        print(f"알 수 없는 상품: {unknown}", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"DB: {db_path}")
    print(f"프로필: {args.profile}")
    print(f"주기: {args.interval}")
    print("모드: 월물별 (차월물→근월물→과거)")
    print(f"세션: {','.join(sessions)} (day=F, night=CM)")
    print("동작: 현재→DB마지막 갱신", end="")
    if args.extend:
        print(" + 과거 확장")
        if args.interval in ("1d", "both"):
            print(f"일봉 확장 하한: {DAILY_START_DATE}")
    else:
        print(" (최근만)")
    if args.no_past:
        print("과거 월물 추정: OFF (전광판만)")
    else:
        print("과거 월물 추정: ON")

    conn = sqlite3.connect(db_path)
    try:
        if args.reset:
            print("\n=== DB 초기화 ===")
            reset_db(conn)
        else:
            init_db(conn)

        for product_key in product_keys:
            label = PRODUCTS[product_key]["label"]
            print(f"\n=== {label} ===")
            try:
                results = sync_product_contracts(
                    conn,
                    profile=args.profile,
                    product_key=product_key,
                    interval=args.interval,
                    extend=args.extend,
                    include_past=not args.no_past,
                    sessions=sessions,
                    verbose=args.verbose,
                )
            except Exception as exc:
                print(f"  FAIL - {exc}", file=sys.stderr)
                continue

            for entry in results:
                month = entry.get("contract_month") or "-"
                symbol = entry.get("symbol") or "-"
                name = entry.get("name") or ""
                title = f"{symbol} ({month})"
                if name:
                    title = f"{title} {name}"
                print(f"\n  {title}")
                if entry.get("error"):
                    print(f"    ERROR: {entry['error']}", file=sys.stderr)
                    continue
                _print_interval_result("1m", entry.get("minute"))
                _print_interval_result("1d", entry.get("daily"))
    finally:
        conn.close()

    print("\n완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
