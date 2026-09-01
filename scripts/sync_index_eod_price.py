#!/usr/bin/env python3
"""
Sync ClickHouse index end-of-day prices into MySQL, then alert via DingTalk.

Designed to run standalone under crontab. On each run it:

1. Reads the current (non-superseded, ``sys_to = '2299-12-31 ...'``) rows from
   the ClickHouse ``dwd_index_eod_price`` table for the configured index code(s)
   (default ``000985.CSI`` — 中证全指) with ``trade_date >= --start``.
2. Upserts them into a MySQL ``index_eod_price`` table, overwriting by primary
   key ``(ts_code, trade_date)`` and stamping each row with a ``synced_at``
   write time.
3. Checks whether MySQL now has the latest fetched trade date. If the window
   returned no rows (or the latest bar failed to land), sends a DingTalk
   **alert**; on success it sends a normal DingTalk message. Any sync failure
   also sends an alert.

Historical + daily in one idempotent run: each run re-syncs the whole
``trade_date >= --start`` window and overwrites by primary key. Point ``--start``
at the historical backfill date once (default ``2026-08-01``); running the same
command daily on cron then keeps only the newest bar(s) changing while safely
re-affirming the rest. There is no separate backfill vs. daily mode.

The ClickHouse read, MySQL upsert, and shared argparse groups live in
``scripts._ch_mysql_sync``; this script only supplies the index-price-specific
columns, WHERE clause, table DDL, and messaging.

DingTalk credentials (``DINGTALK_ACCESS_TOKEN`` / ``DINGTALK_SECRET``) and the
ClickHouse/MySQL connection settings are read from the environment. A ``.env``
file is loaded first, resolved as: ``--env-file`` → ``<script dir>/.env`` →
``<cwd>/.env`` (first existing file wins).

This is infrastructure plumbing (reference-data sync + ops alerting), not
strategy logic, so it talks to ClickHouse/MySQL directly — the same exception
the repo already makes for ``scripts/full_tick_snapshot_to_clickhouse.py``.

Run from the repo root so packages import as top-level::

    # First-time historical backfill from 2026-08-01 (also fine to just run daily):
    python -m scripts.sync_index_eod_price --start 2026-08-01
    python -m scripts.sync_index_eod_price --dry-run
    python -m scripts.sync_index_eod_price --codes 000985.CSI,000300.SH

Example crontab (weekday evenings, after the upstream EOD refresh)::

    23 18 * * 1-5  cd /data/flc/code/quant/qapp && python -m scripts.sync_index_eod_price >> logs/sync_index_eod_price.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from monitoring.dingtalk_alert import DingTalkAlerter  # noqa: E402
from monitoring.dingtalk_alert import load_env  # noqa: E402
from scripts._ch_mysql_sync import CURRENT_VERSION_SYS_TO  # noqa: E402
from scripts._ch_mysql_sync import ClickHouseConn  # noqa: E402
from scripts._ch_mysql_sync import SyncColumn  # noqa: E402
from scripts._ch_mysql_sync import add_behavior_args  # noqa: E402
from scripts._ch_mysql_sync import add_clickhouse_args  # noqa: E402
from scripts._ch_mysql_sync import add_dingtalk_args  # noqa: E402
from scripts._ch_mysql_sync import add_env_file_arg  # noqa: E402
from scripts._ch_mysql_sync import add_mysql_connection_args  # noqa: E402
from scripts._ch_mysql_sync import clickhouse_select_json_each_row  # noqa: E402
from scripts._ch_mysql_sync import connect_mysql  # noqa: E402
from scripts._ch_mysql_sync import preparse_env_file  # noqa: E402
from scripts._ch_mysql_sync import quote_ch_identifier  # noqa: E402
from scripts._ch_mysql_sync import quote_ch_literal  # noqa: E402
from scripts._ch_mysql_sync import upsert_rows  # noqa: E402
from scripts._ch_mysql_sync import _env  # noqa: E402

_LOGGER = logging.getLogger("sync_index_eod_price")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# MySQL target table. Business columns (OHLCV + pct_chg/vol/amount) + a stamp.
MYSQL_TABLE = "index_eod_price"


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# MySQL column <- ClickHouse field mapping. ts_code + trade_date form the PK.
# The order here is also the ClickHouse SELECT order.
_PRICE_FIELDS = ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount")
COLUMNS = [
    SyncColumn("ts_code", is_pk=True, transform=lambda v: str(v) if v is not None else ""),
    SyncColumn("trade_date", is_pk=True),
    *[SyncColumn(name, transform=_float_or_none) for name in _PRICE_FIELDS],
    SyncColumn("exchange", transform=lambda v: str(v) if v is not None else ""),
    SyncColumn("available_trade_date"),
]


def _parse_codes(raw: str) -> list[str]:
    return [c.strip().upper() for c in str(raw or "").split(",") if c.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_env_file_arg(parser)
    # Index selection
    parser.add_argument(
        "--codes",
        default=_env("INDEX_EOD_CODES", "000985.CSI"),
        help="Comma-separated index ts_code(s) to sync (empty string = all codes).",
    )
    parser.add_argument(
        "--start",
        default=_env("INDEX_EOD_START", "2026-08-01"),
        help="Only sync trade_date >= this YYYY-MM-DD (historical backfill anchor).",
    )
    # ClickHouse (source)
    add_clickhouse_args(parser)
    parser.add_argument(
        "--clickhouse-table",
        default=_env("INDEX_EOD_CH_TABLE", "dwd_index_eod_price"),
    )
    # MySQL (target)
    add_mysql_connection_args(parser)
    parser.add_argument("--mysql-table", default=_env("INDEX_EOD_MYSQL_TABLE", MYSQL_TABLE))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(_env("INDEX_EOD_BATCH_SIZE", "1000") or "1000"),
        help="Rows per MySQL upsert executemany batch.",
    )
    # DingTalk
    add_dingtalk_args(parser)
    # Behavior
    add_behavior_args(parser)
    return parser


# ---------------------------------------------------------------------------
# ClickHouse read
# ---------------------------------------------------------------------------
def fetch_index_prices(args: argparse.Namespace, codes: list[str]) -> list[dict[str, Any]]:
    """Read current-version index EOD rows from ClickHouse over HTTP."""
    where = [f"sys_to = {quote_ch_literal(CURRENT_VERSION_SYS_TO)}"]
    if codes:
        code_list = ", ".join(quote_ch_literal(c) for c in codes)
        where.append(f"ts_code IN ({code_list})")
    if args.start:
        where.append(f"trade_date >= {quote_ch_literal(args.start)}")
    select_cols = ", ".join(quote_ch_identifier(c.source_key) for c in COLUMNS)
    sql = (
        f"SELECT {select_cols}\n"
        f"FROM {quote_ch_identifier(args.clickhouse_table)}\n"
        f"WHERE {' AND '.join(where)}\n"
        "ORDER BY ts_code, trade_date\n"
        "FORMAT JSONEachRow"
    )
    return clickhouse_select_json_each_row(ClickHouseConn.from_args(args), sql)


# ---------------------------------------------------------------------------
# MySQL write
# ---------------------------------------------------------------------------
def _create_table_sql(table: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n"
        "  `ts_code` VARCHAR(24) NOT NULL,\n"
        "  `trade_date` DATE NOT NULL,\n"
        "  `open` DECIMAL(20,6) NULL,\n"
        "  `high` DECIMAL(20,6) NULL,\n"
        "  `low` DECIMAL(20,6) NULL,\n"
        "  `close` DECIMAL(20,6) NULL,\n"
        "  `pre_close` DECIMAL(20,6) NULL,\n"
        "  `change` DECIMAL(20,6) NULL,\n"
        "  `pct_chg` DECIMAL(20,6) NULL,\n"
        "  `vol` DECIMAL(30,4) NULL,\n"
        "  `amount` DECIMAL(30,4) NULL,\n"
        "  `exchange` VARCHAR(16) NOT NULL DEFAULT '',\n"
        "  `available_trade_date` DATE NULL,\n"
        "  `synced_at` DATETIME NOT NULL,\n"
        "  PRIMARY KEY (`ts_code`, `trade_date`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def count_on_date(connection, table: str, codes: list[str], trade_date: str) -> int:
    sql = f"SELECT COUNT(*) FROM `{table}` WHERE `trade_date` = %s"
    params: list[Any] = [trade_date]
    if codes:
        placeholders = ", ".join(["%s"] * len(codes))
        sql += f" AND `ts_code` IN ({placeholders})"
        params.extend(codes)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        (count,) = cursor.fetchone()
    return int(count)


def _latest_trade_date(rows: list[dict[str, Any]]) -> str:
    return max(str(r.get("trade_date")) for r in rows)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # Load .env FIRST so every _env(...) default in build_parser() sees it.
    load_env(preparse_env_file(argv), script_dir=Path(__file__).resolve().parent)

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    codes = _parse_codes(args.codes)
    codes_label = ", ".join(codes) if codes else "ALL"

    alerter = DingTalkAlerter(
        access_token=args.access_token or _env("DINGTALK_ACCESS_TOKEN"),
        secret=args.secret or _env("DINGTALK_SECRET"),
        timeout=args.dingtalk_timeout_secs,
    )

    now = datetime.now(SHANGHAI_TZ)

    try:
        _LOGGER.info("reading index prices from ClickHouse table %s", args.clickhouse_table)
        rows = fetch_index_prices(args, codes)
        _LOGGER.info("fetched %d index-price rows (codes=%s start=%s)", len(rows), codes_label, args.start)

        if args.dry_run:
            _LOGGER.info("--dry-run: skipping MySQL write and DingTalk")
            if rows:
                _LOGGER.info("sample row: %s", json.dumps(rows[0], ensure_ascii=False))
                _LOGGER.info("latest trade_date in fetched rows: %s", _latest_trade_date(rows))
            return 0

        connection = connect_mysql(args)
        try:
            if not args.no_create_table:
                with connection.cursor() as cursor:
                    cursor.execute(_create_table_sql(args.mysql_table))
                connection.commit()
                _LOGGER.info("ensured MySQL table %s exists", args.mysql_table)

            sent = upsert_rows(connection, args.mysql_table, COLUMNS, rows, now, args.batch_size)
            _LOGGER.info("upserted %d rows into %s", sent, args.mysql_table)

            latest_date = _latest_trade_date(rows) if rows else None
            latest_count = count_on_date(connection, args.mysql_table, codes, latest_date) if latest_date else 0
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 - alert on any failure, then fail loudly.
        _LOGGER.exception("index EOD price sync failed")
        alerter.send_text(
            f"同步失败: {exc!r}\n时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)",
            title="[qapp] 指数行情同步告警",
        )
        return 2

    if not rows:
        _LOGGER.warning("no index-price rows fetched (codes=%s start=%s)", codes_label, args.start)
        alerter.send_text(
            (
                f"同步完成但未取到任何行情!\n"
                f"指数: {codes_label}  起始: {args.start}\n"
                f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"
            ),
            title="[qapp] 指数行情同步告警",
        )
        return 1

    if latest_count <= 0:
        _LOGGER.warning("latest trade_date %s missing in MySQL after upsert", latest_date)
        alerter.send_text(
            (
                f"同步完成但缺少最新交易日记录!\n"
                f"指数: {codes_label}  最新交易日: {latest_date}\n"
                f"已同步行数: {sent}\n"
                f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"
            ),
            title="[qapp] 指数行情同步告警",
        )
        return 1

    _LOGGER.info("latest trade_date %s present in MySQL (%d row(s))", latest_date, latest_count)
    alerter.send_text(
        (
            f"同步完成\n"
            f"指数: {codes_label}\n"
            f"已同步行数: {sent}  最新交易日: {latest_date} ({latest_count} 条)\n"
            f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"
        ),
        title="[qapp] 指数行情同步",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
