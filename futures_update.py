"""선물 1분봉/일봉 SQLite 갱신."""

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
    sync_daily_product,
    sync_minute_product,
)
from futures.futures_products import DAILY_START_DATE, PRODUCTS

DEFAULT_PRODUCTS = "kospi200,kosdaq150"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="선물 1분봉/일봉 SQLite 갱신")
    parser.add_argument("--profile", default="mock_futures")
    parser.add_argument(
        "--mode",
        choices=("near", "continuous"),
        default="continuous",
        help="near=근월물, continuous=연결선물 (기본: continuous)",
    )
    parser.add_argument(
        "--interval",
        choices=("1m", "1d", "both"),
        default="both",
        help="갱신 주기 (기본: both)",
    )
    parser.add_argument(
        "--extend",
        action="store_true",
        help="최근 갱신 후 과거 구간 확장 (1m: 서버 한도까지, 1d: 20150615까지)",
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
        "--db",
        default=str(DB_PATH),
        help="SQLite DB 경로 (기본: db/futures.db)",
    )
    return parser.parse_args()


def _print_bounds(before_first, before_last, after_first, after_last) -> None:
    if before_first or before_last:
        print(f"  DB(전): {before_first or '-'} ~ {before_last or '-'}")
    else:
        print("  DB(전): (비어 있음)")
    if after_first or after_last:
        print(f"  DB(후): {after_first or '-'} ~ {after_last or '-'}")
    else:
        print("  DB(후): (변화 없음)")


def main() -> int:
    args = parse_args()
    product_keys = [key.strip() for key in args.products.split(",") if key.strip()]
    unknown = [key for key in product_keys if key not in PRODUCTS]
    if unknown:
        print(f"알 수 없는 상품: {unknown}", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"DB: {db_path}")
    print(f"프로필: {args.profile}")
    print(f"모드: {args.mode}")
    print(f"주기: {args.interval}")
    print("동작: 현재→DB마지막 갱신", end="")
    if args.extend:
        print(" + 과거 확장")
        if args.interval in ("1d", "both"):
            print(f"일봉 확장 하한: {DAILY_START_DATE}")
    else:
        print(" (최근만)")

    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        for product_key in product_keys:
            label = PRODUCTS[product_key]["label"]
            print(f"\n=== {label} ===")

            if args.interval in ("1m", "both"):
                print("[1분봉]")
                try:
                    symbol, saved, new_first, new_last, old_first, old_last = (
                        sync_minute_product(
                            conn,
                            profile=args.profile,
                            product_key=product_key,
                            mode=args.mode,
                            extend=args.extend,
                            verbose=args.verbose,
                        )
                    )
                    print(f"  종목: {symbol}, 저장 {saved}건")
                    _print_bounds(old_first, old_last, new_first, new_last)
                except Exception as exc:
                    print(f"  FAIL - {exc}", file=sys.stderr)

            if args.interval in ("1d", "both"):
                print("[일봉]")
                try:
                    symbol, saved, new_first, new_last, old_first, old_last = (
                        sync_daily_product(
                            conn,
                            profile=args.profile,
                            product_key=product_key,
                            mode=args.mode,
                            extend=args.extend,
                            verbose=args.verbose,
                        )
                    )
                    print(f"  종목: {symbol}, 저장 {saved}건")
                    _print_bounds(old_first, old_last, new_first, new_last)
                except Exception as exc:
                    print(f"  FAIL - {exc}", file=sys.stderr)
    finally:
        conn.close()

    print("\n완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
