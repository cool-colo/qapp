#!/usr/bin/env python3
"""
Sync the ClickHouse trade calendar into MySQL, then alert via DingTalk.

Designed to run standalone under crontab. On each run it:

1. Reads the current (non-superseded, ``sys_to = '2299-12-31 ...'``) trade
   calendar rows from the ClickHouse ``dwd_trade_calendar`` table.
2. Upserts them into a MySQL ``trade_calendar`` table, overwriting by primary
   key ``(exchange, cal_date)`` and stamping each row with a ``synced_at`` write
   time.
3. Checks whether MySQL now has a row for *today* (Asia/Shanghai). If it does
   not, sends a DingTalk **alert**; on success it sends a normal DingTalk
   message. Any sync failure also sends an alert.

The ClickHouse read, MySQL upsert, and shared argparse groups live in
``scripts._ch_mysql_sync``; this script only supplies the calendar-specific
columns, WHERE clause, table DDL, and messaging.

DingTalk credentials (``DINGTALK_ACCESS_TOKEN`` / ``DINGTALK_SECRET``) and the
ClickHouse/MySQL connection settings are read from the environment. A ``.env``
file is loaded first, resolved as: ``--env-file`` → ``<script dir>/.env`` →
``<cwd>/.env`` (first existing file wins).

This is infrastructure plumbing (reference-data sync + ops alerting), not
strategy logic, so it talks to ClickHouse/MySQL directly — the same exception
the repo already makes for ``scripts/full_tick_snapshot_to_clickhouse.py``.

Run from the repo root so packages import as top-level::

    python -m scripts.sync_trade_calendar
    python -m scripts.sync_trade_calendar --dry-run
    python -m scripts.sync_trade_calendar --exchange SSE --start 2015-01-01

Example crontab (every morning, after the upstream calendar refresh)::

    17 8 * * *  cd /data/flc/code/quant/qapp && python -m scripts.sync_trade_calendar >> logs/sync_trade_calendar.log 2>&1
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
from scripts._ch_mysql_sync import quote_ch_literal  # noqa: E402
from scripts._ch_mysql_sync import upsert_rows  # noqa: E402
from scripts._ch_mysql_sync import _env  # noqa: E402

_LOGGER = logging.getLogger("sync_trade_calendar")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# MySQL target table. Core calendar columns + a write-time stamp.
MYSQL_TABLE = "trade_calendar"

# MySQL column <- ClickHouse field mapping. exchange + cal_date form the PK.
COLUMNS = [
    SyncColumn("exchange", is_pk=True, transform=lambda v: str(v) if v is not None else ""),
    SyncColumn("cal_date", is_pk=True),
    SyncColumn("is_open", transform=lambda v: int(v) if v not in (None, "") else 0),
    SyncColumn("pretrade_date"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_env_file_arg(parser)
    # Calendar selection
    parser.add_argument(
        "--exchange",
        default=_env("TRADE_CALENDAR_EXCHANGE", "SSE"),
        help="Exchange to sync (empty string = all exchanges).",
    )
    parser.add_argument(
        "--start",
        default=_env("TRADE_CALENDAR_START", "2015-01-01"),
        help="Only sync cal_date >= this YYYY-MM-DD.",
    )
    # ClickHouse (source)
    add_clickhouse_args(parser)
    parser.add_argument(
        "--clickhouse-table",
        default=_env("TRADE_CALENDAR_CH_TABLE", "dwd_trade_calendar"),
    )
    # MySQL (target)
    add_mysql_connection_args(parser)
    parser.add_argument("--mysql-table", default=_env("TRADE_CALENDAR_MYSQL_TABLE", MYSQL_TABLE))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(_env("TRADE_CALENDAR_BATCH_SIZE", "1000") or "1000"),
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
def fetch_calendar(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Read current-version calendar rows from ClickHouse over HTTP."""
    where = [f"sys_to = {quote_ch_literal(CURRENT_VERSION_SYS_TO)}"]
    if args.exchange:
        where.append(f"exchange = {quote_ch_literal(args.exchange)}")
    if args.start:
        where.append(f"cal_date >= {quote_ch_literal(args.start)}")
    sql = (
        "SELECT exchange, cal_date, is_open, pretrade_date\n"
        f"FROM `{args.clickhouse_table}`\n"
        f"WHERE {' AND '.join(where)}\n"
        "ORDER BY exchange, cal_date\n"
        "FORMAT JSONEachRow"
    )
    return clickhouse_select_json_each_row(ClickHouseConn.from_args(args), sql)


# ---------------------------------------------------------------------------
# MySQL write
# ---------------------------------------------------------------------------
def _create_table_sql(table: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n"
        "  `exchange` VARCHAR(16) NOT NULL,\n"
        "  `cal_date` DATE NOT NULL,\n"
        "  `is_open` TINYINT NOT NULL,\n"
        "  `pretrade_date` DATE NULL,\n"
        "  `synced_at` DATETIME NOT NULL,\n"
        "  PRIMARY KEY (`exchange`, `cal_date`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def count_today(connection, table: str, exchange: str, today: str) -> int:
    sql = f"SELECT COUNT(*) FROM `{table}` WHERE `cal_date` = %s"
    params: list[Any] = [today]
    if exchange:
        sql += " AND `exchange` = %s"
        params.append(exchange)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        (count,) = cursor.fetchone()
    return int(count)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # Load .env FIRST so every _env(...) default in build_parser() sees it
    # (host/user/password/etc. are resolved at parser-construction time).
    load_env(preparse_env_file(argv), script_dir=Path(__file__).resolve().parent)

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    alerter = DingTalkAlerter(
        access_token=args.access_token or _env("DINGTALK_ACCESS_TOKEN"),
        secret=args.secret or _env("DINGTALK_SECRET"),
        timeout=args.dingtalk_timeout_secs,
    )

    now = datetime.now(SHANGHAI_TZ)
    today = now.strftime("%Y-%m-%d")

    try:
        _LOGGER.info("reading calendar from ClickHouse table %s", args.clickhouse_table)
        rows = fetch_calendar(args)
        _LOGGER.info("fetched %d calendar rows (exchange=%s start=%s)", len(rows), args.exchange or "*", args.start)

        if args.dry_run:
            _LOGGER.info("--dry-run: skipping MySQL write and DingTalk")
            if rows:
                _LOGGER.info("sample row: %s", json.dumps(rows[0], ensure_ascii=False))
            has_today = any(str(r.get("cal_date")) == today for r in rows)
            _LOGGER.info("today %s present in fetched rows: %s", today, has_today)
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

            today_count = count_today(connection, args.mysql_table, args.exchange, today)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 - alert on any failure, then fail loudly.
        _LOGGER.exception("trade calendar sync failed")
        alerter.send_text(
            f"同步失败: {exc!r}\n时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)",
            title="[qapp] 交易日历同步告警",
        )
        return 2

    exchange_label = args.exchange or "ALL"
    if today_count <= 0:
        _LOGGER.warning("no MySQL calendar row for today %s (exchange=%s)", today, exchange_label)
        alerter.send_text(
            (
                f"同步完成但缺少当天记录!\n"
                f"日期: {today}  交易所: {exchange_label}\n"
                f"已同步行数: {sent}\n"
                f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"
            ),
            title="[qapp] 交易日历同步告警",
        )
        return 1

    _LOGGER.info("today %s present in MySQL (%d row(s))", today, today_count)
    alerter.send_text(
        (
            f"同步完成\n"
            f"日期: {today}  交易所: {exchange_label}\n"
            f"已同步行数: {sent}  当天记录数: {today_count}\n"
            f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"
        ),
        title="[qapp] 交易日历同步",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
