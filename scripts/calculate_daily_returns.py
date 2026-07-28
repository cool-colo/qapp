#!/usr/bin/env python3
"""Calculate and persist daily realised/unrealised stock returns.

The calculation is intentionally an operations/reporting script.  It reads the
live MySQL snapshots and trades, reads yesterday's close from ClickHouse, and
writes one idempotent result table in MySQL.  It does not participate in any
strategy or order-routing decision.

For each account/trader/stock/day the generated rows are:

* ``buy``: ``(close - fill_price) * bought_qty - commission``
* ``sell``: ``(fill_price - pre_close) * sold_qty - commission - stamp_tax``
* ``hold``: ``(close - pre_close) * unchanged_qty``
* ``all``: the sum of the above rows

``unchanged_qty`` is the portion of the closing position that was not bought
today (``after_trading volume - today's buy volume``).  It is reconciled with
``before_trading volume - today's sell volume``; a discrepancy fails loudly so
that a partial/missing snapshot is not silently reported as P&L.

Usage::

    python -m scripts.calculate_daily_returns --trade-date 2026-07-27
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitoring.dingtalk_alert import load_env  # noqa: E402

LOGGER = logging.getLogger("calculate_daily_returns")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
COMMISSION_RATE = Decimal("0.0001")  # 万 1
MIN_COMMISSION = Decimal("5")
STAMP_TAX_RATE = Decimal("0.00005")  # 万 5
MONEY_QUANTUM = Decimal("0.0001")
RATE_QUANTUM = Decimal("0.00000001")
TRADE_CALENDAR_TABLE = "trade_calendar"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _rate(value: Decimal, base: Decimal) -> Decimal | None:
    if base <= ZERO:
        return None
    return (value / base).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _normalise_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"BUY", "B", "买入"}:
        return "buy"
    if text in {"SELL", "S", "卖出"}:
        return "sell"
    raise ValueError(f"unsupported live_trade side: {value!r}")


def _symbol_keys(*values: Any) -> set[str]:
    """Return exact and six-digit keys, tolerating source code formatting."""
    keys: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip().upper()
        if not text:
            continue
        keys.add(text)
        digits = "".join(char for char in text if char.isdigit())
        if len(digits) >= 6:
            keys.add(digits[:6])
    return keys


def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["account_id"]),
        str(row["trader_id"]),
        str(row["instrument_id"]),
    )


def _price_lookup(rows: list[dict[str, Any]], column: str) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        for key in _symbol_keys(row.get("stock_code"), row.get("instrument_id"), row.get("symbol")):
            prices[key] = _decimal(value)
    return prices


def _find_price(prices: dict[str, Decimal], *symbols: Any) -> Decimal | None:
    for key in _symbol_keys(*symbols):
        if key in prices:
            return prices[key]
    return None


def _weighted_price(trades: list[dict[str, Any]]) -> tuple[int, Decimal, Decimal]:
    quantity = sum(int(row.get("quantity") or 0) for row in trades)
    if quantity <= 0:
        return 0, ZERO, ZERO
    amount = sum(
        _decimal(row.get("amount"))
        if row.get("amount") is not None
        else _decimal(row.get("price")) * int(row.get("quantity") or 0)
        for row in trades
    )
    return quantity, amount / Decimal(quantity), amount


def calculate_records(
    trade_date: date | str,
    trades: list[dict[str, Any]],
    before_positions: list[dict[str, Any]],
    after_positions: list[dict[str, Any]],
    ticks: list[dict[str, Any]],
    eod_prices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build detail and summary rows without database side effects.

    This public, pure function is also the calculation boundary for tests and
    makes the financial convention explicit independently of the SQL readers.
    """
    trade_date_text = str(trade_date)
    before_by_key = {_row_key(row): row for row in before_positions}
    after_by_key = {_row_key(row): row for row in after_positions}
    trades_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        trades_by_key[_row_key(row)].append(row)

    close_prices = _price_lookup(ticks, "last_price")
    open_prices = _price_lookup(ticks, "open")
    pre_close_prices = _price_lookup(eod_prices, "pre_close")
    all_keys = set(before_by_key) | set(after_by_key) | set(trades_by_key)
    details: list[dict[str, Any]] = []

    for key in sorted(all_keys):
        account_id, trader_id, instrument_id = key
        before = before_by_key.get(key)
        after = after_by_key.get(key)
        stock_code = str(
            (after or before or (trades_by_key[key][0] if trades_by_key[key] else {})).get("stock_code")
            or instrument_id
        )
        instrument_trades = trades_by_key.get(key, [])
        buys = [row for row in instrument_trades if _normalise_side(row.get("side")) == "buy"]
        sells = [row for row in instrument_trades if _normalise_side(row.get("side")) == "sell"]
        buy_qty, buy_price, buy_amount = _weighted_price(buys)
        sell_qty, sell_price, sell_amount = _weighted_price(sells)
        before_qty = int((before or {}).get("volume") or 0)
        after_qty = int((after or {}).get("volume") or 0)
        # Some broker snapshots retain zero-volume rows after a position is
        # closed.  They are neither a holding nor a trade and must not require a
        # tick/price or produce a zero-return stock record.
        if before_qty == 0 and after_qty == 0 and buy_qty == 0 and sell_qty == 0:
            continue
        unchanged_from_after = max(0, after_qty - buy_qty)
        unchanged_from_before = max(0, before_qty - sell_qty)
        if before is not None and after is not None and unchanged_from_after != unchanged_from_before:
            raise ValueError(
                f"position reconciliation failed for {account_id}/{trader_id}/{instrument_id} on "
                f"{trade_date_text}: before={before_qty}, buy={buy_qty}, sell={sell_qty}, after={after_qty}",
            )
        unchanged_qty = unchanged_from_after if after is not None else unchanged_from_before
        close = _find_price(close_prices, stock_code, instrument_id)
        pre_close = _find_price(pre_close_prices, stock_code, instrument_id)
        open_price = _find_price(open_prices, stock_code, instrument_id)
        # The after-trading tick recorder persists held instruments.  A stock sold
        # completely during the day therefore has no tick row, which is fine: the
        # sell formula deliberately uses only fill price and pre-close.
        if (buy_qty or unchanged_qty) and close is None:
            raise ValueError(f"missing today close in live_stock_tick_snapshot for {instrument_id} on {trade_date_text}")
        if (sell_qty or unchanged_qty) and pre_close is None:
            raise ValueError(f"missing pre_close in dwd_stock_eod_price for {instrument_id} on {trade_date_text}")

        cost_price = _decimal((before or after or {}).get("avg_price"), default=ZERO) or None
        common = {
            "trade_date": trade_date_text,
            "account_id": account_id,
            "trader_id": trader_id,
            "instrument_id": instrument_id,
            "stock_code": stock_code,
            "cost_price": cost_price,
            "pre_close": pre_close,
            "close_price": close,
            "open_price": open_price,
        }
        rows: list[dict[str, Any]] = []
        if buy_qty:
            fee = max(_money(buy_amount * COMMISSION_RATE), MIN_COMMISSION)
            pnl = _money((close - buy_price) * buy_qty - fee)
            rows.append({
                **common, "calculation_type": "buy", "quantity": buy_qty, "trade_price": buy_price,
                "fee": fee, "tax": ZERO, "return_amount": pnl,
                "reference_amount": _money(buy_amount), "return_rate": _rate(pnl, buy_amount),
            })
        if sell_qty:
            fee = max(_money(sell_amount * COMMISSION_RATE), MIN_COMMISSION)
            tax = _money(sell_amount * STAMP_TAX_RATE)
            reference = pre_close * sell_qty  # guaranteed above
            pnl = _money((sell_price - pre_close) * sell_qty - fee - tax)  # type: ignore[operator]
            rows.append({
                **common, "calculation_type": "sell", "quantity": sell_qty, "trade_price": sell_price,
                "fee": fee, "tax": tax, "return_amount": pnl,
                "reference_amount": _money(reference), "return_rate": _rate(pnl, reference),
            })
        if unchanged_qty:
            reference = pre_close * unchanged_qty  # guaranteed above
            pnl = _money((close - pre_close) * unchanged_qty)  # type: ignore[operator]
            rows.append({
                **common, "calculation_type": "hold", "quantity": unchanged_qty, "trade_price": None,
                "fee": ZERO, "tax": ZERO, "return_amount": pnl,
                "reference_amount": _money(reference), "return_rate": _rate(pnl, reference),
            })
        if rows:
            total_reference = sum((_decimal(row["reference_amount"]) for row in rows), ZERO)
            total_return = sum((_decimal(row["return_amount"]) for row in rows), ZERO)
            rows.append({
                **common, "calculation_type": "all", "quantity": sum(int(row["quantity"]) for row in rows),
                "trade_price": None, "fee": _money(sum((_decimal(row["fee"]) for row in rows), ZERO)),
                "tax": _money(sum((_decimal(row["tax"]) for row in rows), ZERO)),
                "return_amount": _money(total_return), "reference_amount": _money(total_reference),
                "return_rate": _rate(total_return, total_reference),
            })
            details.extend(rows)

    summaries: list[dict[str, Any]] = []
    by_account: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in details:
        if row["calculation_type"] == "all":
            by_account[(row["account_id"], row["trader_id"])].append(row)
    for (account_id, trader_id), rows in sorted(by_account.items()):
        reference = sum((_decimal(row["reference_amount"]) for row in rows), ZERO)
        pnl = sum((_decimal(row["return_amount"]) for row in rows), ZERO)
        summaries.append({
            "trade_date": trade_date_text, "account_id": account_id, "trader_id": trader_id,
            # ``all`` keeps the type vocabulary identical to a per-stock total;
            # the stock/instrument value distinguishes the portfolio summary.
            "instrument_id": "summary", "stock_code": "summary", "calculation_type": "all",
            "quantity": sum(int(row["quantity"]) for row in rows), "cost_price": None,
            "trade_price": None, "pre_close": None, "close_price": None, "open_price": None,
            "fee": _money(sum((_decimal(row["fee"]) for row in rows), ZERO)),
            "tax": _money(sum((_decimal(row["tax"]) for row in rows), ZERO)),
            "return_amount": _money(pnl), "reference_amount": _money(reference),
            "return_rate": _rate(pnl, reference),
        })
    return details + summaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    parser.add_argument("--trade-date", default=_env("DAILY_RETURN_TRADE_DATE"), help="YYYY-MM-DD; defaults to today in Asia/Shanghai.")
    parser.add_argument("--account-id", default=_env("DAILY_RETURN_ACCOUNT_ID"))
    parser.add_argument("--trader-id", default=_env("DAILY_RETURN_TRADER_ID"))
    parser.add_argument("--mysql-host", default=_env("MYSQL_HOST", "localhost"))
    parser.add_argument("--mysql-port", type=int, default=int(_env("MYSQL_PORT", "3306") or "3306"))
    parser.add_argument("--mysql-user", default=_env("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=_env("MYSQL_PASSWORD", ""))
    parser.add_argument("--mysql-database", default=_env("MYSQL_DATABASE", "backtest"))
    parser.add_argument("--mysql-result-table", default=_env("DAILY_RETURN_MYSQL_TABLE", "live_daily_stock_return"))
    parser.add_argument("--clickhouse-url", default=_env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=_env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=_env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=_env("CLICKHOUSE_PASSWORD"))
    parser.add_argument("--clickhouse-timeout-secs", type=float, default=float(_env("CLICKHOUSE_TIMEOUT_SECS", "30") or "30"))
    parser.add_argument("--eod-table", default=_env("DAILY_RETURN_EOD_TABLE", "dwd_stock_eod_price"))
    parser.add_argument("--eod-date-column", default=_env("DAILY_RETURN_EOD_DATE_COLUMN", "trade_date"))
    parser.add_argument("--eod-pre-close-column", default=_env("DAILY_RETURN_EOD_PRE_CLOSE_COLUMN", "pre_close"))
    parser.add_argument("--no-create-table", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=_env("QMT_LOG_LEVEL", "INFO"))
    return parser


def _connect_mysql(args: argparse.Namespace):
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise ImportError("pymysql is required to calculate daily returns") from exc
    return pymysql.connect(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                           password=args.mysql_password, database=args.mysql_database,
                           charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def _filters(args: argparse.Namespace, prefix: str = "") -> tuple[str, list[Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if args.account_id:
        filters.append(f"{prefix}`account_id` = %s")
        params.append(args.account_id)
    if args.trader_id:
        filters.append(f"{prefix}`trader_id` = %s")
        params.append(args.trader_id)
    return (" AND " + " AND ".join(filters)) if filters else "", params


def fetch_mysql_inputs(connection, args: argparse.Namespace, trade_date: str) -> tuple[list[dict[str, Any]], ...]:
    scope, scope_params = _filters(args)
    columns = "`account_id`, `trader_id`, `instrument_id`, `stock_code`, `volume`, `avg_price`"
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {columns} FROM `live_position_snapshot` WHERE `trade_date` = %s "
            "AND `snapshot_type` = 'before_trading'" + scope,
            [trade_date, *scope_params],
        )
        before = list(cursor.fetchall())
        # Do not use the most recent snapshot before ``trade_date``: a gap in
        # snapshot recording must not turn an old position into today's opening
        # position.  The requested convention is precisely the *previous trading
        # day*, which is maintained by scripts.sync_trade_calendar.
        cursor.execute(
            f"SELECT MAX(`pretrade_date`) AS `pretrade_date` FROM `{TRADE_CALENDAR_TABLE}` "
            "WHERE `cal_date` = %s AND `is_open` = 1",
            (trade_date,),
        )
        calendar_row = cursor.fetchone()
        previous_trade_date = (calendar_row or {}).get("pretrade_date")
        if previous_trade_date is None:
            raise ValueError(
                f"no previous trading day found for {trade_date} in `{TRADE_CALENDAR_TABLE}`; "
                "run scripts.sync_trade_calendar first",
            )
        cursor.execute(
            f"SELECT {columns} FROM `live_position_snapshot` WHERE `snapshot_type` = 'after_trading' "
            "AND `trade_date` = %s" + scope,
            [previous_trade_date, *scope_params],
        )
        fallback = list(cursor.fetchall())
        seen = {_row_key(row) for row in before}
        for row in fallback:
            if _row_key(row) not in seen:
                before.append(row)
                seen.add(_row_key(row))
        cursor.execute(
            f"SELECT {columns} FROM `live_position_snapshot` WHERE `trade_date` = %s "
            "AND `snapshot_type` = 'after_trading'" + scope,
            [trade_date, *scope_params],
        )
        after = list(cursor.fetchall())
        cursor.execute(
            "SELECT `account_id`, `trader_id`, `instrument_id`, `stock_code`, `side`, `price`, `quantity`, `amount` "
            "FROM `live_trade` WHERE `trade_date` = %s" + scope,
            [trade_date, *scope_params],
        )
        trades = list(cursor.fetchall())
        cursor.execute(
            "SELECT `instrument_id`, `stock_code`, `last_price`, `open` FROM `live_stock_tick_snapshot` "
            "WHERE `trade_date` = %s AND `snapshot_type` = 'after_trading'",
            (trade_date,),
        )
        ticks = list(cursor.fetchall())
    return trades, before, after, ticks


def _ch_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or not value or value[0].isdigit():
        raise ValueError(f"invalid ClickHouse identifier: {value!r}")
    return f"`{value}`"


def fetch_eod_prices(args: argparse.Namespace, trade_date: str) -> list[dict[str, Any]]:
    table = _ch_identifier(args.eod_table)
    symbol = _ch_identifier("ts_code")
    date_column = _ch_identifier(args.eod_date_column)
    pre_close = _ch_identifier(args.eod_pre_close_column)
    sql = f"SELECT {symbol} AS symbol, {pre_close} AS pre_close FROM {table} WHERE {date_column} = '{trade_date}' FORMAT JSONEachRow"
    params: dict[str, str] = {"query": sql}
    if args.clickhouse_database:
        params["database"] = args.clickhouse_database
    headers = {"Accept": "application/json"}
    if args.clickhouse_user:
        headers["X-ClickHouse-User"] = args.clickhouse_user
    if args.clickhouse_password:
        headers["X-ClickHouse-Key"] = args.clickhouse_password
    request = urllib.request.Request(f"{args.clickhouse_url.rstrip('/')}/?{urlencode(params)}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=args.clickhouse_timeout_secs) as response:
            return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ClickHouse HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ClickHouse request failed: {exc}") from exc


def create_table(connection, table: str) -> None:
    sql = f"""CREATE TABLE IF NOT EXISTS `{table}` (
  `trade_date` DATE NOT NULL, `account_id` VARCHAR(64) NOT NULL, `trader_id` VARCHAR(64) NOT NULL,
  `instrument_id` VARCHAR(32) NOT NULL, `stock_code` VARCHAR(16) NOT NULL,
  `type` VARCHAR(16) NOT NULL, `quantity` BIGINT NOT NULL,
  `cost_price` DECIMAL(20,4) NULL, `trade_price` DECIMAL(20,4) NULL,
  `pre_close` DECIMAL(20,4) NULL, `close_price` DECIMAL(20,4) NULL, `open_price` DECIMAL(20,4) NULL,
  `fee` DECIMAL(20,4) NOT NULL, `tax` DECIMAL(20,4) NOT NULL,
  `return_amount` DECIMAL(20,4) NOT NULL, `reference_amount` DECIMAL(20,4) NOT NULL,
  `return_rate` DECIMAL(20,8) NULL, `created_at` DATETIME NOT NULL, `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`trade_date`, `account_id`, `trader_id`, `instrument_id`, `type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    with connection.cursor() as cursor:
        cursor.execute(sql)
    connection.commit()


def upsert_records(connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = ("trade_date", "account_id", "trader_id", "instrument_id", "stock_code", "type", "quantity",
               "cost_price", "trade_price", "pre_close", "close_price", "open_price", "fee", "tax", "return_amount",
               "reference_amount", "return_rate")
    names = ", ".join(f"`{column}`" for column in columns)
    values = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"`{column}` = VALUES(`{column}`)" for column in columns if column not in {"trade_date", "account_id", "trader_id", "instrument_id", "type"})
    sql = f"INSERT INTO `{table}` ({names}, `created_at`, `updated_at`) VALUES ({values}, %s, %s) ON DUPLICATE KEY UPDATE {updates}, `updated_at` = VALUES(`updated_at`)"
    now = datetime.now()
    params = [
        tuple(row["calculation_type"] if column == "type" else row[column] for column in columns) + (now, now)
        for row in rows
    ]
    with connection.cursor() as cursor:
        cursor.executemany(sql, params)
    connection.commit()


def _preparse_env_file(argv: list[str] | None) -> str | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    known, _ = parser.parse_known_args(argv)
    return known.env_file


def main(argv: list[str] | None = None) -> int:
    load_env(_preparse_env_file(argv), script_dir=Path(__file__).resolve().parent)
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    trade_date = args.trade_date or datetime.now(SHANGHAI_TZ).date().isoformat()
    try:
        date.fromisoformat(trade_date)
    except ValueError as exc:
        raise SystemExit(f"--trade-date must be YYYY-MM-DD: {trade_date!r}") from exc
    connection = _connect_mysql(args)
    try:
        trades, before, after, ticks = fetch_mysql_inputs(connection, args, trade_date)
        eod = fetch_eod_prices(args, trade_date)
        rows = calculate_records(trade_date, trades, before, after, ticks, eod)
        LOGGER.info("calculated %d daily-return rows from %d trades, %d before positions, %d after positions", len(rows), len(trades), len(before), len(after))
        if args.dry_run:
            LOGGER.info("--dry-run: skipping MySQL result write")
            for row in rows:
                LOGGER.info("%s", json.dumps(row, ensure_ascii=False, default=str, sort_keys=True))
            return 0
        if not args.no_create_table:
            create_table(connection, args.mysql_result_table)
        upsert_records(connection, args.mysql_result_table, rows)
        LOGGER.info("upserted %d rows into %s", len(rows), args.mysql_result_table)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
