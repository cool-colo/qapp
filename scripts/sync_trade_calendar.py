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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
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

_LOGGER = logging.getLogger("sync_trade_calendar")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# The current-version marker in the SCD-2 ClickHouse calendar table.
CURRENT_VERSION_SYS_TO = "2299-12-31 00:00:00.000"

# MySQL target table. Core calendar columns + a write-time stamp.
MYSQL_TABLE = "trade_calendar"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--env-file",
        default=_env("QAPP_ENV_FILE"),
        help="Explicit .env path (else <script dir>/.env, else <cwd>/.env).",
    )
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
    parser.add_argument("--clickhouse-url", default=_env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=_env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=_env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=_env("CLICKHOUSE_PASSWORD"))
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(_env("CLICKHOUSE_TIMEOUT_SECS", "60") or "60"),
    )
    parser.add_argument(
        "--clickhouse-table",
        default=_env("TRADE_CALENDAR_CH_TABLE", "dwd_trade_calendar"),
    )
    # MySQL (target)
    parser.add_argument("--mysql-host", default=_env("MYSQL_HOST", "localhost"))
    parser.add_argument("--mysql-port", type=int, default=int(_env("MYSQL_PORT", "3306") or "3306"))
    parser.add_argument("--mysql-user", default=_env("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=_env("MYSQL_PASSWORD", ""))
    parser.add_argument("--mysql-database", default=_env("MYSQL_DATABASE", "backtest"))
    parser.add_argument("--mysql-table", default=_env("TRADE_CALENDAR_MYSQL_TABLE", MYSQL_TABLE))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(_env("TRADE_CALENDAR_BATCH_SIZE", "1000") or "1000"),
        help="Rows per MySQL upsert executemany batch.",
    )
    # DingTalk
    parser.add_argument("--access-token", default=_env("DINGTALK_ACCESS_TOKEN"))
    parser.add_argument("--secret", default=_env("DINGTALK_SECRET"))
    parser.add_argument(
        "--dingtalk-timeout-secs",
        type=float,
        default=float(_env("DINGTALK_TIMEOUT_SECS", "5") or "5"),
    )
    # Behavior
    parser.add_argument(
        "--no-create-table",
        action="store_true",
        help="Skip CREATE TABLE IF NOT EXISTS for the MySQL table.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + log counts but do not write MySQL or send DingTalk.",
    )
    parser.add_argument("--log-level", default=_env("QMT_LOG_LEVEL", "INFO"))
    return parser


# ---------------------------------------------------------------------------
# ClickHouse read
# ---------------------------------------------------------------------------
def _quote_ch_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fetch_calendar(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Read current-version calendar rows from ClickHouse over HTTP."""
    where = [f"sys_to = {_quote_ch_literal(CURRENT_VERSION_SYS_TO)}"]
    if args.exchange:
        where.append(f"exchange = {_quote_ch_literal(args.exchange)}")
    if args.start:
        where.append(f"cal_date >= {_quote_ch_literal(args.start)}")
    sql = (
        "SELECT exchange, cal_date, is_open, pretrade_date\n"
        f"FROM `{args.clickhouse_table}`\n"
        f"WHERE {' AND '.join(where)}\n"
        "ORDER BY exchange, cal_date\n"
        "FORMAT JSONEachRow"
    )

    params: dict[str, str] = {"query": sql}
    if args.clickhouse_database:
        params["database"] = args.clickhouse_database
    url = f"{str(args.clickhouse_url).rstrip('/')}/?{urlencode(params)}"
    headers = {"Accept": "application/json"}
    if args.clickhouse_user:
        headers["X-ClickHouse-User"] = args.clickhouse_user
    if args.clickhouse_password:
        headers["X-ClickHouse-Key"] = args.clickhouse_password

    request = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=args.clickhouse_timeout_secs) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ClickHouse HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ClickHouse request failed: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# MySQL write
# ---------------------------------------------------------------------------
def _connect_mysql(args: argparse.Namespace):
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise ImportError("pymysql is required to sync the trade calendar to MySQL") from exc
    return pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
        charset="utf8mb4",
        autocommit=False,
    )


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


def _empty_to_none(value: Any) -> Any:
    # ClickHouse can emit '0000-00-00' / '' for a null Date; treat as NULL.
    if value in (None, "", "0000-00-00"):
        return None
    return value


def upsert_calendar(
    connection,
    table: str,
    rows: list[dict[str, Any]],
    synced_at: datetime,
    batch_size: int,
) -> int:
    """Upsert rows by primary key (exchange, cal_date). Returns rows sent."""
    if not rows:
        return 0
    sql = (
        f"INSERT INTO `{table}` (`exchange`, `cal_date`, `is_open`, `pretrade_date`, `synced_at`)\n"
        "VALUES (%s, %s, %s, %s, %s)\n"
        "ON DUPLICATE KEY UPDATE\n"
        "  `is_open` = VALUES(`is_open`),\n"
        "  `pretrade_date` = VALUES(`pretrade_date`),\n"
        "  `synced_at` = VALUES(`synced_at`)"
    )
    stamp = synced_at.strftime("%Y-%m-%d %H:%M:%S")
    params = [
        (
            str(row.get("exchange", "")),
            _empty_to_none(row.get("cal_date")),
            int(row.get("is_open", 0)),
            _empty_to_none(row.get("pretrade_date")),
            stamp,
        )
        for row in rows
    ]
    sent = 0
    with connection.cursor() as cursor:
        for start in range(0, len(params), max(1, batch_size)):
            batch = params[start:start + max(1, batch_size)]
            cursor.executemany(sql, batch)
            sent += len(batch)
    connection.commit()
    return sent


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
def _preparse_env_file(argv: list[str] | None) -> str | None:
    """Pull out only --env-file before the full parser reads env-var defaults."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    known, _ = pre.parse_known_args(argv)
    return known.env_file


def main(argv: list[str] | None = None) -> int:
    # Load .env FIRST so every _env(...) default in build_parser() sees it
    # (host/user/password/etc. are resolved at parser-construction time).
    load_env(_preparse_env_file(argv), script_dir=Path(__file__).resolve().parent)

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

        connection = _connect_mysql(args)
        try:
            if not args.no_create_table:
                with connection.cursor() as cursor:
                    cursor.execute(_create_table_sql(args.mysql_table))
                connection.commit()
                _LOGGER.info("ensured MySQL table %s exists", args.mysql_table)

            sent = upsert_calendar(connection, args.mysql_table, rows, now, args.batch_size)
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
