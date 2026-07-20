#!/usr/bin/env python3
"""
Whole-market full-tick snapshot -> ClickHouse ODS ingestion.

Designed to run standalone under crontab. On each run it:

1. Enumerates the whole A-share universe (Shanghai + Shenzhen + Beijing) from
   the quant-qmt-proxy ``/sectors`` endpoint (the ``沪深京A股`` sector), the same
   source ``QMTInstrumentProvider`` uses for ``load_all``.
2. Fetches a full-tick snapshot for every symbol from the proxy
   ``/api/v1/data/full-tick`` endpoint (chunked), preserving the five-level
   bid/ask depth arrays.
3. Writes each tick as a row into a ClickHouse ODS table, tagged with the
   ingest wall-clock time so multiple intraday runs accumulate append-only.

This is infrastructure plumbing (market-data capture), not strategy logic, so
it talks to the QMT proxy HTTP API directly — the same exception the repo
already makes for ``lives/sell_all_sellable.py``. It does NOT go through
Nautilus, which has no full-tick data type.

Downstream, a scheduled SQL job dedups the ODS rows of a trading day into the
DWD table (see ``--emit-dwd-sql`` / ``scripts/full_tick_ods_to_dwd.sql``).

Run from the repo root so ``lives``/``backtests`` import as top-level packages::

    python -m scripts.full_tick_snapshot_to_clickhouse
    python -m scripts.full_tick_snapshot_to_clickhouse --dry-run --max-symbols 5
    python -m scripts.full_tick_snapshot_to_clickhouse --emit-dwd-sql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
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

from backtests.data_providers.clickhouse import quote_identifier  # noqa: E402
from lives.live_common import QMT_DEFAULT_HTTP_URL  # noqa: E402
from lives.live_common import chunks  # noqa: E402
from lives.live_common import env  # noqa: E402
from lives.live_common import env_bool  # noqa: E402
from lives.live_common import qmt_symbol  # noqa: E402

_LOGGER = logging.getLogger("full_tick_snapshot")

# The QMT whole-market A-share sector: Shanghai + Shenzhen + Beijing. Matches
# QMTInstrumentProvider.DEFAULT_LOAD_ALL_SECTORS.
WHOLE_MARKET_SECTOR = "沪深京A股"

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# Scalar tick fields (name -> ClickHouse type). Mirrors the proxy's
# _normalize_tick_payload scalar keys.
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

# Five-level order-book depth arrays.
ARRAY_TICK_FIELDS: list[tuple[str, str]] = [
    ("ask_price", "Array(Float64)"),
    ("bid_price", "Array(Float64)"),
    ("ask_vol", "Array(Int64)"),
    ("bid_vol", "Array(Int64)"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # QMT proxy
    parser.add_argument("--base-url-http", default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL))
    parser.add_argument("--api-key", default=env("QMT_API_KEY"))
    parser.add_argument("--sector", default=env("QMT_FULL_TICK_SECTOR", WHOLE_MARKET_SECTOR))
    parser.add_argument(
        "--http-timeout-secs",
        type=float,
        default=float(env("QMT_HTTP_TIMEOUT_SECS", "30")),
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(env("QMT_FULL_TICK_CHUNK_SIZE", "500")),
        help="Symbols per /full-tick request.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=int(env("QMT_FULL_TICK_MAX_SYMBOLS", "0")),
        help="Cap the universe for testing (0 = whole market).",
    )
    # ClickHouse
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
    parser.add_argument("--log-level", default=env("QMT_LOG_LEVEL", "INFO"))
    return parser


# ---------------------------------------------------------------------------
# QMT proxy HTTP
# ---------------------------------------------------------------------------
def _proxy_get(base_url: str, api_key: str | None, path: str, timeout: float) -> Any:
    url = f"{base_url}{path}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    return _read_proxy_json(request, timeout)


def _proxy_post(base_url: str, api_key: str | None, path: str, body: dict[str, Any], timeout: float) -> Any:
    url = f"{base_url}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return _read_proxy_json(request, timeout)


def _read_proxy_json(request: urllib.request.Request, timeout: float, max_attempts: int = 5) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and not payload.get("success", True):
                raise RuntimeError(str(payload.get("message") or payload))
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        except (urllib.error.URLError, ValueError, TimeoutError, RuntimeError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"QMT proxy request {request.get_method()} {request.full_url} "
                    f"failed after {max_attempts} attempts: {exc}"
                ) from exc
            _LOGGER.warning(
                "QMT proxy request failed (attempt %d/%d), retrying in 1s: %s",
                attempt,
                max_attempts,
                exc,
            )
            time.sleep(1.0)
    raise RuntimeError("unreachable")


def load_universe(args: argparse.Namespace) -> list[str]:
    """Enumerate whole-market QMT symbols from the proxy /sectors endpoint."""
    base_url = str(args.base_url_http or "").rstrip("/")
    if not base_url:
        raise SystemExit("--base-url-http (QMT_BASE_URL_HTTP) is required")
    data = _proxy_get(base_url, args.api_key, "/api/v1/data/sectors", args.http_timeout_secs)
    sectors = data.get("items", []) if isinstance(data, dict) else (data or [])
    seen: set[str] = set()
    symbols: list[str] = []
    for sector in sectors:
        if str(sector.get("sector_name", "")) != args.sector:
            continue
        for raw in sector.get("symbols", []) or []:
            symbol = qmt_symbol(str(raw))
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    if not symbols:
        raise SystemExit(
            f"proxy /sectors returned no symbols for sector {args.sector!r}; "
            f"available sectors: {[s.get('sector_name') for s in sectors][:20]}"
        )
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    return symbols


def fetch_full_tick(args: argparse.Namespace, symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch full-tick snapshots for all symbols, chunked. Returns proxy items."""
    base_url = str(args.base_url_http or "").rstrip("/")
    items: list[dict[str, Any]] = []
    for chunk in chunks(symbols, max(1, args.chunk_size)):
        data = _proxy_post(
            base_url,
            args.api_key,
            "/api/v1/data/full-tick",
            {"symbols": chunk},
            args.http_timeout_secs,
        )
        chunk_items = data.get("items", []) if isinstance(data, dict) else (data or [])
        items.extend(chunk_items)
    return items


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
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
    """Convert proxy full-tick items into ODS rows (JSONEachRow shape)."""
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


# ---------------------------------------------------------------------------
# ClickHouse HTTP
# ---------------------------------------------------------------------------
def _clickhouse_execute(args: argparse.Namespace, sql: str, body: bytes | None = None) -> str:
    """POST a statement (DDL) or an INSERT payload to ClickHouse over HTTP."""
    params: dict[str, str] = {}
    if args.clickhouse_database:
        params["database"] = args.clickhouse_database
    if body is not None:
        # For INSERT ... FORMAT JSONEachRow we pass the statement via the `query`
        # param and the rows as the request body.
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
    # Append-only ODS: MergeTree, partitioned by day, ordered by symbol+ingest.
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
        _clickhouse_execute(args, insert_sql, body=payload.encode("utf-8"))
        written += len(batch)
    return written


# ---------------------------------------------------------------------------
# ODS -> DWD dedup/sync SQL
# ---------------------------------------------------------------------------
def dwd_sync_sql(ods_table: str, dwd_table: str) -> str:
    """
    Idempotent ODS->DWD sync for one trading day: keep the latest ingest per
    (trade_date, symbol), replace the day's DWD partition.

    Intended to run once after market close (e.g. via crontab), parameterized by
    the {date} placeholder (a 'YYYY-MM-DD' string).
    """
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

    insert_columns = ", ".join(
        ["trade_date", "symbol", "ingest_time", *value_cols]
    )

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


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.emit_dwd_sql:
        print(dwd_sync_sql(args.ods_table, args.dwd_table))
        return 0

    now = datetime.now(SHANGHAI_TZ)

    _LOGGER.info("loading whole-market universe (sector=%s)", args.sector)
    symbols = load_universe(args)
    _LOGGER.info("universe: %d symbols", len(symbols))

    _LOGGER.info("fetching full-tick snapshot in chunks of %d", args.chunk_size)
    items = fetch_full_tick(args, symbols)
    _LOGGER.info("full-tick returned %d items for %d requested symbols", len(items), len(symbols))

    rows = build_rows(items, now)
    _LOGGER.info("assembled %d ODS rows", len(rows))

    if args.dry_run:
        _LOGGER.info("--dry-run: skipping ClickHouse write")
        if rows:
            _LOGGER.info("sample row: %s", json.dumps(rows[0], ensure_ascii=False))
        return 0

    if not rows:
        _LOGGER.warning("no rows to write; exiting without touching ClickHouse")
        return 0

    if not args.no_create_table:
        _clickhouse_execute(args, create_ods_table_sql(args.ods_table))
        _LOGGER.info("ensured ODS table %s exists", args.ods_table)

    written = insert_rows(args, rows)
    _LOGGER.info("wrote %d rows into %s", written, args.ods_table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
