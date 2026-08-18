"""Shared ClickHouse persistence for full-tick snapshot collectors."""
from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from backtests.data_providers.clickhouse import quote_identifier
from lives.live_common import env
from lives.live_common import env_bool


_LOGGER = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# The normalized ODS contract shared by the QMT proxy and Big QMT collectors.
SCALAR_TICK_FIELDS: list[tuple[str, str]] = [
    ("time_ms", "Int64"),
    ("last_price", "Float64"),
    ("open", "Float64"),
    ("high", "Float64"),
    ("low", "Float64"),
    ("last_close", "Float64"),
    ("amount", "Float64"),
    ("volume", "Int64"),
    ("pvolume", "Int64"),
    ("open_int", "Int64"),
    ("stock_status", "Int32"),
    ("last_settlement_price", "Float64"),
    ("transaction_num", "Int64"),
]

ARRAY_TICK_FIELDS: list[tuple[str, str]] = [
    ("ask_price", "Array(Float64)"),
    ("bid_price", "Array(Float64)"),
    ("ask_vol", "Array(Int64)"),
    ("bid_vol", "Array(Int64)"),
]


def chunks(items: list[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def add_clickhouse_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clickhouse-url", default=env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=env("CLICKHOUSE_PASSWORD"))
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(env("CLICKHOUSE_TIMEOUT_SECS", "60")),
    )
    parser.add_argument(
        "--ods-table",
        default=env("FULL_TICK_ODS_TABLE", "ods_stock_full_tick_snapshot"),
    )
    parser.add_argument(
        "--dwd-table",
        default=env("FULL_TICK_DWD_TABLE", "dwd_stock_full_tick_snapshot"),
    )
    parser.add_argument(
        "--insert-batch-size",
        type=int,
        default=int(env("FULL_TICK_INSERT_BATCH_SIZE", "5000")),
        help="Rows per ClickHouse INSERT request.",
    )
    parser.add_argument(
        "--no-create-table",
        action="store_true",
        default=env_bool("FULL_TICK_NO_CREATE_TABLE", False),
        help="Skip CREATE TABLE IF NOT EXISTS for the ODS table.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("FULL_TICK_DRY_RUN", False),
        help="Fetch and log counts but do not write to ClickHouse.",
    )
    parser.add_argument(
        "--emit-dwd-sql",
        action="store_true",
        help="Print the ODS->DWD dedup/sync SQL for the current tables and exit.",
    )


def _coerce_scalar(value: Any, ch_type: str) -> Any:
    try:
        if ch_type.startswith("Int"):
            return int(float(value))
        return float(value)
    except (TypeError, ValueError):
        return 0


def _coerce_array(value: Any, ch_type: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    is_int = "Int" in ch_type
    out: list[Any] = []
    for item in value:
        try:
            out.append(int(float(item)) if is_int else float(item))
        except (TypeError, ValueError):
            out.append(0 if is_int else 0.0)
    return out


def build_rows(
    items: list[dict[str, Any]],
    ingest_time: datetime,
) -> list[dict[str, Any]]:
    """Convert normalized full-tick items into ODS rows (JSONEachRow shape)."""
    trade_date = ingest_time.strftime("%Y-%m-%d")
    ingest_ts = ingest_time.strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol", "")).strip().upper()
        tick = item.get("tick")
        if not symbol or not isinstance(tick, dict):
            _LOGGER.warning("skipping unusable full-tick item: %s", item)
            continue
        row: dict[str, Any] = {
            "trade_date": trade_date,
            "symbol": symbol,
            "ingest_time": ingest_ts,
        }
        for name, ch_type in SCALAR_TICK_FIELDS:
            row[name] = _coerce_scalar(tick.get(name, 0), ch_type)
        for name, ch_type in ARRAY_TICK_FIELDS:
            row[name] = _coerce_array(tick.get(name), ch_type)
        rows.append(row)
    return rows


def clickhouse_execute(args: argparse.Namespace, sql: str, body: bytes | None = None) -> str:
    """POST a statement (DDL) or an INSERT payload to ClickHouse over HTTP."""
    params: dict[str, str] = {}
    if args.clickhouse_database:
        params["database"] = args.clickhouse_database
    if body is not None:
        params["query"] = sql
    url = str(args.clickhouse_url).rstrip("/")
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if args.clickhouse_user:
        headers["X-ClickHouse-User"] = args.clickhouse_user
    if args.clickhouse_password:
        headers["X-ClickHouse-Key"] = args.clickhouse_password
    data = body if body is not None else sql.encode("utf-8")
    request = urllib.request.Request(url=url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.clickhouse_timeout_secs) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ClickHouse HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ClickHouse request failed: {exc}") from exc


def create_ods_table_sql(table: str) -> str:
    columns = [
        "    `trade_date` Date",
        "    `symbol` String",
        "    `ingest_time` DateTime",
    ]
    for name, ch_type in SCALAR_TICK_FIELDS:
        columns.append(f"    `{name}` {ch_type}")
    for name, ch_type in ARRAY_TICK_FIELDS:
        columns.append(f"    `{name}` {ch_type}")
    columns_sql = ",\n".join(columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(table)} (\n"
        f"{columns_sql}\n"
        ")\n"
        "ENGINE = MergeTree\n"
        "PARTITION BY trade_date\n"
        "ORDER BY (symbol, ingest_time)"
    )


def insert_rows(args: argparse.Namespace, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    insert_sql = f"INSERT INTO {quote_identifier(args.ods_table)} FORMAT JSONEachRow"
    written = 0
    for batch in chunks(rows, max(1, args.insert_batch_size)):
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in batch)
        clickhouse_execute(args, insert_sql, body=payload.encode("utf-8"))
        written += len(batch)
    return written


def dwd_sync_sql(ods_table: str, dwd_table: str) -> str:
    """Build the idempotent per-day ODS-to-DWD deduplication SQL."""
    scalar_cols = [name for name, _ in SCALAR_TICK_FIELDS]
    array_cols = [name for name, _ in ARRAY_TICK_FIELDS]
    value_cols = scalar_cols + array_cols
    ods = quote_identifier(ods_table)
    dwd = quote_identifier(dwd_table)

    dwd_columns = [
        "    `trade_date` Date",
        "    `symbol` String",
        "    `ingest_time` DateTime",
    ]
    for name, ch_type in SCALAR_TICK_FIELDS:
        dwd_columns.append(f"    `{name}` {ch_type}")
    for name, ch_type in ARRAY_TICK_FIELDS:
        dwd_columns.append(f"    `{name}` {ch_type}")
    dwd_columns_sql = ",\n".join(dwd_columns)
    argmax_cols = ",\n".join(
        ["    max(ingest_time) AS ingest_time_max"]
        + [f"    argMax({col}, ingest_time) AS {col}" for col in value_cols]
    )
    insert_columns = ", ".join(["trade_date", "symbol", "ingest_time", *value_cols])

    return f"""-- ODS -> DWD full-tick dedup/sync for a single trading day.
-- Replace {{date}} with the target 'YYYY-MM-DD' before running.
-- Dedup rule: keep the row with the latest ingest_time per (trade_date, symbol).

CREATE TABLE IF NOT EXISTS {dwd} (
{dwd_columns_sql}
)
ENGINE = ReplacingMergeTree(ingest_time)
PARTITION BY trade_date
ORDER BY (trade_date, symbol);

-- Idempotent per-day replace: drop then reload the day's partition.
ALTER TABLE {dwd} DROP PARTITION '{{date}}';

INSERT INTO {dwd} ({insert_columns})
SELECT
    trade_date,
    symbol,
{argmax_cols}
FROM {ods}
WHERE trade_date = '{{date}}'
GROUP BY trade_date, symbol;
"""
