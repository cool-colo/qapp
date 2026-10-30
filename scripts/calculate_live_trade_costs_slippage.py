#!/usr/bin/env python3
"""Report live trading costs and buy/sell slippage by account and trader.

Trading costs are calculated from ``live_trade.price * live_trade.quantity``:

* commission: 0.00875% on buys and sells (万分之 0.875)
* stamp tax: 0.05% on sells only (万分之 5)
* transfer fee: 0.001% on buys and sells (万分之 0.1)

Buy and sell slippage are reported separately with a consistent sign convention:
negative is favorable and positive is unfavorable. Buy slippage is the
quantity-weighted ``fill price - open price``; sell slippage is the
quantity-weighted ``open price - fill price``. Slippage in basis points (bps)
divides the signed aggregate slippage amount by aggregate actual turnover
``fill price * quantity`` and multiplies by 10,000. The open price is resolved from the matching
``live_order`` first (client order id, then venue order id), with
``live_stock_tick_snapshot.open`` as a fallback.  Trades without a positive open
price remain in the fee calculation and are explicitly counted, but are excluded
from the slippage averages.

By default the script reads the allow-listed ``MYSQL_*`` exports from
``start_big_qmt_live_trading_5.sh``.  Existing environment variables and explicit
CLI arguments take precedence; the shell script is parsed as text and never run.

Examples::

    python -m scripts.calculate_live_trade_costs_slippage
    python -m scripts.calculate_live_trade_costs_slippage \
        --start-date 2026-10-01 --end-date 2026-10-31 --format csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from decimal import ROUND_HALF_UP
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitoring.dingtalk_alert import load_env  # noqa: E402

COMMISSION_RATE = Decimal("0.0000875")
STAMP_TAX_RATE = Decimal("0.0005")
TRANSFER_FEE_RATE = Decimal("0.00001")
MONEY_QUANTUM = Decimal("0.0001")
PRICE_QUANTUM = Decimal("0.000001")
BPS_QUANTUM = Decimal("0.000001")
ZERO = Decimal("0")
MYSQL_EXPORT_NAMES = {
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
}
DEFAULT_MYSQL_CONFIG_SCRIPT = PROJECT_ROOT / "start_big_qmt_live_trading_5.sh"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "output" / "live_trade_costs_slippage.csv"

OUTPUT_COLUMNS = (
    "account_id",
    "trader_id",
    "trade_count",
    "buy_quantity",
    "sell_quantity",
    "buy_turnover",
    "sell_turnover",
    "commission_fee",
    "stamp_tax",
    "transfer_fee",
    "total_trading_fee",
    "buy_slippage_trade_count",
    "buy_missing_open_trade_count",
    "buy_weighted_fill_price",
    "buy_weighted_open_price",
    "buy_slippage",
    "buy_slippage_bps",
    "sell_slippage_trade_count",
    "sell_missing_open_trade_count",
    "sell_weighted_fill_price",
    "sell_weighted_open_price",
    "sell_slippage",
    "sell_slippage_bps",
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def _load_mysql_exports(path: str | os.PathLike[str] | None) -> None:
    """Load only literal MYSQL_* exports without executing the launcher."""
    if not path:
        return
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"MySQL config script does not exist: {config_path}")
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export "):].strip()
        name, separator, raw_value = assignment.partition("=")
        name = name.strip()
        if not separator or name not in MYSQL_EXPORT_NAMES:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _preload_environment(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    parser.add_argument(
        "--mysql-config-script",
        default=_env("TRADE_REPORT_MYSQL_CONFIG_SCRIPT", str(DEFAULT_MYSQL_CONFIG_SCRIPT)),
    )
    known, _ = parser.parse_known_args(argv)
    load_env(known.env_file, script_dir=Path(__file__).resolve().parent)
    _load_mysql_exports(known.mysql_config_script)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=_env("QAPP_ENV_FILE"))
    parser.add_argument(
        "--mysql-config-script",
        default=_env("TRADE_REPORT_MYSQL_CONFIG_SCRIPT", str(DEFAULT_MYSQL_CONFIG_SCRIPT)),
        help="Launcher whose literal MYSQL_* exports are used as connection defaults.",
    )
    parser.add_argument("--mysql-host", default=_env("MYSQL_HOST", "localhost"))
    parser.add_argument("--mysql-port", type=int, default=int(_env("MYSQL_PORT", "3306") or "3306"))
    parser.add_argument("--mysql-user", default=_env("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=_env("MYSQL_PASSWORD", ""))
    parser.add_argument("--mysql-database", default=_env("MYSQL_DATABASE", "backtest"))
    parser.add_argument("--start-date", default=_env("TRADE_REPORT_START_DATE"), help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--end-date", default=_env("TRADE_REPORT_END_DATE"), help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--account-id", default=_env("TRADE_REPORT_ACCOUNT_ID"))
    parser.add_argument("--trader-id", default=_env("TRADE_REPORT_TRADER_ID"))
    parser.add_argument("--format", choices=("table", "csv", "json"), default="csv")
    parser.add_argument(
        "--output",
        default=_env("TRADE_REPORT_OUTPUT", str(DEFAULT_OUTPUT_PATH)),
        help="Result file path; use '-' to write to stdout.",
    )
    return parser


def _validate_dates(args: argparse.Namespace) -> None:
    start = date.fromisoformat(args.start_date) if args.start_date else None
    end = date.fromisoformat(args.end_date) if args.end_date else None
    if start is not None and end is not None and start > end:
        raise ValueError(f"--start-date {start} must not be after --end-date {end}")


def _query_scope(
    *,
    start_date: str | None,
    end_date: str | None,
    account_id: str | None,
    trader_id: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if start_date:
        clauses.append("`trade_date` >= %s")
        params.append(start_date)
    if end_date:
        clauses.append("`trade_date` <= %s")
        params.append(end_date)
    if account_id:
        clauses.append("`account_id` = %s")
        params.append(account_id)
    if trader_id:
        clauses.append("`trader_id` = %s")
        params.append(trader_id)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def fetch_inputs(connection: Any, args: argparse.Namespace) -> tuple[list[dict[str, Any]], ...]:
    scope, params = _query_scope(
        start_date=args.start_date,
        end_date=args.end_date,
        account_id=args.account_id,
        trader_id=args.trader_id,
    )
    trade_columns = (
        "`trade_date`, `account_id`, `trader_id`, `trade_id`, `client_order_id`, "
        "`venue_order_id`, `instrument_id`, `side`, `price`, `quantity`"
    )
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {trade_columns} FROM `live_trade`{scope}", params)
        trades = list(cursor.fetchall())
        if not trades:
            return [], [], []

        first_date = min(row["trade_date"] for row in trades)
        last_date = max(row["trade_date"] for row in trades)
        order_scope, order_params = _query_scope(
            start_date=str(first_date),
            end_date=str(last_date),
            account_id=args.account_id,
            trader_id=args.trader_id,
        )
        cursor.execute(
            "SELECT `trade_date`, `account_id`, `trader_id`, `client_order_id`, `venue_order_id`, `open_price` "
            f"FROM `live_order`{order_scope}",
            order_params,
        )
        orders = list(cursor.fetchall())
        cursor.execute(
            "SELECT `trade_date`, `instrument_id`, MIN(NULLIF(`open`, 0)) AS `min_open`, "
            "MAX(NULLIF(`open`, 0)) AS `max_open` FROM `live_stock_tick_snapshot` "
            "WHERE `trade_date` BETWEEN %s AND %s GROUP BY `trade_date`, `instrument_id`",
            (first_date, last_date),
        )
        ticks = list(cursor.fetchall())
    return trades, orders, ticks


def _add_open_price(target: dict[tuple[Any, ...], Decimal], key: tuple[Any, ...], value: Any) -> None:
    if value is None:
        return
    open_price = _decimal(value)
    if open_price <= ZERO:
        return
    existing = target.get(key)
    if existing is not None and existing != open_price:
        raise ValueError(f"ambiguous open prices for key {key!r}: {existing} and {open_price}")
    target[key] = open_price


def build_open_price_maps(
    orders: Iterable[Mapping[str, Any]],
    ticks: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[tuple[Any, str, str, str], Decimal],
    dict[tuple[Any, str, str, str], Decimal],
    dict[tuple[Any, str], Decimal],
]:
    client_opens: dict[tuple[Any, str, str, str], Decimal] = {}
    venue_opens: dict[tuple[Any, str, str, str], Decimal] = {}
    tick_opens: dict[tuple[Any, str], Decimal] = {}
    for row in orders:
        account_id = str(row["account_id"])
        trader_id = str(row["trader_id"])
        client_order_id = str(row.get("client_order_id") or "")
        venue_order_id = str(row.get("venue_order_id") or "")
        if client_order_id:
            _add_open_price(
                client_opens,
                (row["trade_date"], account_id, trader_id, client_order_id),
                row.get("open_price"),
            )
        if venue_order_id:
            _add_open_price(
                venue_opens,
                (row["trade_date"], account_id, trader_id, venue_order_id),
                row.get("open_price"),
            )
    for row in ticks:
        min_open = row.get("min_open")
        max_open = row.get("max_open")
        if min_open is not None and max_open is not None and _decimal(min_open) != _decimal(max_open):
            raise ValueError(
                "ambiguous tick open prices for "
                f"{row['trade_date']}/{row['instrument_id']}: {min_open} and {max_open}",
            )
        _add_open_price(
            tick_opens,
            (row["trade_date"], str(row["instrument_id"])),
            max_open,
        )
    return client_opens, venue_opens, tick_opens


def _new_totals() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "buy_quantity": 0,
        "sell_quantity": 0,
        "buy_turnover": ZERO,
        "sell_turnover": ZERO,
        "buy_slippage_trade_count": 0,
        "sell_slippage_trade_count": 0,
        "buy_missing_open_trade_count": 0,
        "sell_missing_open_trade_count": 0,
        "buy_slippage_quantity": 0,
        "sell_slippage_quantity": 0,
        "buy_fill_value": ZERO,
        "sell_fill_value": ZERO,
        "buy_open_value": ZERO,
        "sell_open_value": ZERO,
    }


def calculate_grouped_report(
    trades: Iterable[Mapping[str, Any]],
    orders: Iterable[Mapping[str, Any]],
    ticks: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Calculate one report row per ``(account_id, trader_id)``."""
    client_opens, venue_opens, tick_opens = build_open_price_maps(orders, ticks)
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(_new_totals)

    for row in trades:
        account_id = str(row["account_id"])
        trader_id = str(row["trader_id"])
        side = str(row.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported live_trade side for trade {row.get('trade_id')!r}: {row.get('side')!r}")
        price = _decimal(row.get("price"))
        quantity = int(row.get("quantity") or 0)
        if price <= ZERO or quantity <= 0:
            raise ValueError(
                f"invalid price/quantity for trade {row.get('trade_id')!r}: price={price}, quantity={quantity}",
            )
        side_key = side.lower()
        totals = grouped[(account_id, trader_id)]
        turnover = price * quantity
        totals["trade_count"] += 1
        totals[f"{side_key}_quantity"] += quantity
        totals[f"{side_key}_turnover"] += turnover

        client_order_id = str(row.get("client_order_id") or "")
        venue_order_id = str(row.get("venue_order_id") or "")
        order_key_prefix = (row["trade_date"], account_id, trader_id)
        open_price = client_opens.get((*order_key_prefix, client_order_id)) if client_order_id else None
        if open_price is None and venue_order_id:
            open_price = venue_opens.get((*order_key_prefix, venue_order_id))
        if open_price is None:
            open_price = tick_opens.get((row["trade_date"], str(row["instrument_id"])))
        if open_price is None:
            totals[f"{side_key}_missing_open_trade_count"] += 1
            continue
        totals[f"{side_key}_slippage_trade_count"] += 1
        totals[f"{side_key}_slippage_quantity"] += quantity
        totals[f"{side_key}_fill_value"] += turnover
        totals[f"{side_key}_open_value"] += open_price * quantity

    report: list[dict[str, Any]] = []
    for (account_id, trader_id), totals in sorted(grouped.items()):
        buy_turnover = totals["buy_turnover"]
        sell_turnover = totals["sell_turnover"]
        total_turnover = buy_turnover + sell_turnover
        commission = total_turnover * COMMISSION_RATE
        stamp_tax = sell_turnover * STAMP_TAX_RATE
        transfer_fee = total_turnover * TRANSFER_FEE_RATE
        result: dict[str, Any] = {
            "account_id": account_id,
            "trader_id": trader_id,
            "trade_count": totals["trade_count"],
            "buy_quantity": totals["buy_quantity"],
            "sell_quantity": totals["sell_quantity"],
            "buy_turnover": _money(buy_turnover),
            "sell_turnover": _money(sell_turnover),
            "commission_fee": _money(commission),
            "stamp_tax": _money(stamp_tax),
            "transfer_fee": _money(transfer_fee),
            "total_trading_fee": _money(commission + stamp_tax + transfer_fee),
            "buy_slippage_trade_count": totals["buy_slippage_trade_count"],
            "buy_missing_open_trade_count": totals["buy_missing_open_trade_count"],
            "sell_slippage_trade_count": totals["sell_slippage_trade_count"],
            "sell_missing_open_trade_count": totals["sell_missing_open_trade_count"],
        }
        for side in ("buy", "sell"):
            quantity = totals[f"{side}_slippage_quantity"]
            if quantity <= 0:
                result[f"{side}_weighted_fill_price"] = None
                result[f"{side}_weighted_open_price"] = None
                result[f"{side}_slippage"] = None
                result[f"{side}_slippage_bps"] = None
                continue
            fill_value = totals[f"{side}_fill_value"]
            open_value = totals[f"{side}_open_value"]
            weighted_fill = fill_value / quantity
            weighted_open = open_value / quantity
            result[f"{side}_weighted_fill_price"] = _price(weighted_fill)
            result[f"{side}_weighted_open_price"] = _price(weighted_open)
            signed_slippage_amount = (
                fill_value - open_value
                if side == "buy"
                else open_value - fill_value
            )
            result[f"{side}_slippage"] = _price(signed_slippage_amount / quantity)
            result[f"{side}_slippage_bps"] = (
                (signed_slippage_amount / fill_value * Decimal("10000")).quantize(
                    BPS_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )
            )
        report.append(result)
    return report


def _display_value(value: Any) -> str:
    return "" if value is None else str(value)


def _write_report_stream(rows: list[dict[str, Any]], output_format: str, stream: Any) -> None:
    if output_format == "json":
        print(json.dumps(rows, ensure_ascii=False, default=str, indent=2), file=stream)
        return
    if output_format == "csv":
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    rendered = [[_display_value(row.get(column)) for column in OUTPUT_COLUMNS] for row in rows]
    widths = [len(column) for column in OUTPUT_COLUMNS]
    for values in rendered:
        widths = [max(width, len(value)) for width, value in zip(widths, values)]
    print("  ".join(column.ljust(width) for column, width in zip(OUTPUT_COLUMNS, widths)), file=stream)
    print("  ".join("-" * width for width in widths), file=stream)
    for values in rendered:
        print("  ".join(value.ljust(width) for value, width in zip(values, widths)), file=stream)


def write_report(rows: list[dict[str, Any]], output_format: str, output: str | os.PathLike[str]) -> Path | None:
    if str(output) == "-":
        _write_report_stream(rows, output_format, sys.stdout)
        return None
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        _write_report_stream(rows, output_format, stream)
    return output_path


def _connect_mysql(args: argparse.Namespace) -> Any:
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise ImportError("pymysql is required to report live trade costs and slippage") from exc
    return pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def main(argv: list[str] | None = None) -> int:
    _preload_environment(argv)
    args = build_parser().parse_args(argv)
    try:
        _validate_dates(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    connection = _connect_mysql(args)
    try:
        trades, orders, ticks = fetch_inputs(connection, args)
    finally:
        connection.close()
    rows = calculate_grouped_report(trades, orders, ticks)
    output_path = write_report(rows, args.format, args.output)
    if output_path is not None:
        print(f"wrote {len(rows)} account/trader rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
