#!/usr/bin/env python3
"""Reusable ClickHouse → MySQL sync core.

This module holds the strategy-agnostic plumbing shared by the crontab-driven
reference-data sync scripts (``scripts.sync_trade_calendar``,
``scripts.sync_index_eod_price``, …):

* reading the *current* (non-superseded) version of an SCD-2 ClickHouse table
  over the HTTP interface as ``JSONEachRow``,
* connecting to MySQL and upserting rows by primary key (overwrite-by-PK) with
  a ``synced_at`` write-time stamp,
* the shared argparse groups (ClickHouse connection, MySQL connection, DingTalk,
  common behavior flags) and the ``--env-file`` pre-parse.

Each concrete sync script supplies only what is dataset-specific: the source
table, the column spec, the ``WHERE`` predicate, the ``CREATE TABLE`` DDL, and
the post-sync freshness check / DingTalk messaging.

Like ``scripts.full_tick_snapshot_to_clickhouse`` and
``scripts.sync_trade_calendar``, this is infrastructure plumbing (reference-data
sync + ops alerting), not trading-strategy logic, so it talks to
ClickHouse/MySQL directly rather than through Nautilus.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

# The current-version marker in the SCD-2 ClickHouse dwd_* tables. A row whose
# ``sys_to`` equals this sentinel is the live version; superseded versions carry
# an earlier ``sys_to``.
CURRENT_VERSION_SYS_TO = "2299-12-31 00:00:00.000"


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# Backwards-compatible alias used by the sync scripts.
_env = env


# ---------------------------------------------------------------------------
# ClickHouse literal / identifier quoting
# ---------------------------------------------------------------------------
def quote_ch_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def quote_ch_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# ClickHouse read (HTTP, JSONEachRow)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClickHouseConn:
    url: str = "http://127.0.0.1:8123"
    database: str | None = None
    user: str | None = "default"
    password: str | None = None
    timeout_secs: float = 60.0

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "ClickHouseConn":
        return cls(
            url=str(args.clickhouse_url),
            database=args.clickhouse_database or None,
            user=args.clickhouse_user or None,
            password=args.clickhouse_password or None,
            timeout_secs=float(args.clickhouse_timeout_secs),
        )


def clickhouse_select_json_each_row(conn: ClickHouseConn, sql: str) -> list[dict[str, Any]]:
    """Run ``sql`` (must end with ``FORMAT JSONEachRow``) and return parsed rows."""
    params: dict[str, str] = {"query": sql}
    if conn.database:
        params["database"] = conn.database
    url = f"{conn.url.rstrip('/')}/?{urlencode(params)}"
    headers = {"Accept": "application/json"}
    if conn.user:
        headers["X-ClickHouse-User"] = conn.user
    if conn.password:
        headers["X-ClickHouse-Key"] = conn.password

    request = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=conn.timeout_secs) as response:
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
# MySQL write (pymysql, PK upsert)
# ---------------------------------------------------------------------------
def connect_mysql(args: argparse.Namespace):
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise ImportError("pymysql is required to sync into MySQL") from exc
    return pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
        charset="utf8mb4",
        autocommit=False,
    )


def empty_to_none(value: Any) -> Any:
    # ClickHouse can emit '0000-00-00' / '' for a null Date; treat as NULL.
    if value in (None, "", "0000-00-00"):
        return None
    return value


@dataclass(frozen=True)
class SyncColumn:
    """One MySQL column fed from a ClickHouse source field.

    ``name``      -- MySQL column name (also the source dict key by default).
    ``source``    -- ClickHouse row key, if it differs from ``name``.
    ``is_pk``     -- part of the primary key (excluded from the UPDATE clause).
    ``transform`` -- optional value transform; defaults to ``empty_to_none``.
    """

    name: str
    source: str | None = None
    is_pk: bool = False
    transform: Callable[[Any], Any] | None = None

    @property
    def source_key(self) -> str:
        return self.source or self.name

    def value(self, row: dict[str, Any]) -> Any:
        raw = row.get(self.source_key)
        fn = self.transform if self.transform is not None else empty_to_none
        return fn(raw)


def upsert_rows(
    connection,
    table: str,
    columns: list[SyncColumn],
    rows: list[dict[str, Any]],
    synced_at: datetime,
    batch_size: int,
    synced_column: str = "synced_at",
) -> int:
    """Upsert ``rows`` into ``table`` by primary key. Returns rows sent.

    Builds ``INSERT ... ON DUPLICATE KEY UPDATE`` over ``columns`` plus a
    write-time ``synced_column``; primary-key columns are excluded from the
    UPDATE clause so re-runs overwrite the non-key columns in place.
    """
    if not rows:
        return 0

    all_names = [c.name for c in columns] + [synced_column]
    insert_cols = ", ".join(quote_ch_identifier(n) for n in all_names)
    placeholders = ", ".join(["%s"] * len(all_names))
    update_names = [c.name for c in columns if not c.is_pk] + [synced_column]
    update_clause = ",\n  ".join(
        f"{quote_ch_identifier(n)} = VALUES({quote_ch_identifier(n)})" for n in update_names
    )
    sql = (
        f"INSERT INTO {quote_ch_identifier(table)} ({insert_cols})\n"
        f"VALUES ({placeholders})\n"
        f"ON DUPLICATE KEY UPDATE\n  {update_clause}"
    )

    stamp = synced_at.strftime("%Y-%m-%d %H:%M:%S")
    params = [tuple(c.value(row) for c in columns) + (stamp,) for row in rows]

    step = max(1, batch_size)
    sent = 0
    with connection.cursor() as cursor:
        for start in range(0, len(params), step):
            batch = params[start:start + step]
            cursor.executemany(sql, batch)
            sent += len(batch)
    connection.commit()
    return sent


# ---------------------------------------------------------------------------
# Shared argparse groups
# ---------------------------------------------------------------------------
def add_env_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=_env("QAPP_ENV_FILE"),
        help="Explicit .env path (else <script dir>/.env, else <cwd>/.env).",
    )


def add_clickhouse_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clickhouse-url", default=_env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=_env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=_env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=_env("CLICKHOUSE_PASSWORD"))
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(_env("CLICKHOUSE_TIMEOUT_SECS", "60") or "60"),
    )


def add_mysql_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mysql-host", default=_env("MYSQL_HOST", "localhost"))
    parser.add_argument("--mysql-port", type=int, default=int(_env("MYSQL_PORT", "3306") or "3306"))
    parser.add_argument("--mysql-user", default=_env("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=_env("MYSQL_PASSWORD", ""))
    parser.add_argument("--mysql-database", default=_env("MYSQL_DATABASE", "backtest"))


def add_dingtalk_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--access-token", default=_env("DINGTALK_ACCESS_TOKEN"))
    parser.add_argument("--secret", default=_env("DINGTALK_SECRET"))
    parser.add_argument(
        "--dingtalk-timeout-secs",
        type=float,
        default=float(_env("DINGTALK_TIMEOUT_SECS", "5") or "5"),
    )


def add_behavior_args(parser: argparse.ArgumentParser) -> None:
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


def preparse_env_file(argv: list[str] | None) -> str | None:
    """Pull out only --env-file before the full parser reads env-var defaults."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    known, _ = pre.parse_known_args(argv)
    return known.env_file
