#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from backtests.data_providers import BarDataProvider  # noqa: E402
from backtests.data_providers import ClickHouseBarDataProvider  # noqa: E402
from backtests.data_providers import ClickHouseBarSchema  # noqa: E402
from backtests.data_providers import ClickHouseConnectionConfig  # noqa: E402
from backtests.data_providers import PreparedBarData  # noqa: E402
from backtests.common import add_benchmark_args  # noqa: E402
from backtests.common import benchmark_config_from_args  # noqa: E402
from backtests.common import load_benchmark_returns  # noqa: E402
from backtests.base import BaseBacktest  # noqa: E402


REPORT_PROCESSOR = BaseBacktest()


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the reusable EMA cross strategy in a Nautilus low-level backtest "
            "with QMT as the venue and bars loaded directly from ClickHouse."
        ),
    )
    parser.add_argument(
        "--symbol",
        default=env("QMT_SYMBOL", "600000.SH"),
        help=(
            "Stock symbol. Accepts QMT symbols like 600000.SH, or warehouse IDs "
            "like stock:600000.SH / stock.600000.SH."
        ),
    )
    parser.add_argument(
        "--start",
        default=env("BACKTEST_START"),
        help="Inclusive start date, for example 2024-01-01.",
    )
    parser.add_argument(
        "--end",
        default=env("BACKTEST_END"),
        help="Exclusive end date, for example 2024-02-01.",
    )
    parser.add_argument(
        "--clickhouse-url",
        default=env("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
        help="ClickHouse HTTP URL.",
    )
    parser.add_argument(
        "--clickhouse-database",
        default=env("CLICKHOUSE_DATABASE"),
        help="Optional ClickHouse database selected for the query.",
    )
    parser.add_argument(
        "--clickhouse-user",
        default=env("CLICKHOUSE_USER", "default"),
        help="ClickHouse user.",
    )
    parser.add_argument(
        "--clickhouse-password",
        default=env("CLICKHOUSE_PASSWORD"),
        help="ClickHouse password.",
    )
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(env("CLICKHOUSE_TIMEOUT_SECS", "30")),
        help="ClickHouse HTTP timeout.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(env("CLICKHOUSE_LIMIT", "0")),
        help="Optional maximum number of rows to load. 0 means no limit.",
    )
    parser.add_argument(
        "--exchange-timezone",
        default=env("QMT_EXCHANGE_TIMEZONE", "Asia/Shanghai"),
        help="Timezone applied to naive ClickHouse timestamps.",
    )
    parser.add_argument(
        "--price-precision",
        type=int,
        default=int(env("QMT_PRICE_PRECISION", "2")),
        help="Decimal places used when converting prices.",
    )
    parser.add_argument(
        "--starting-cash",
        type=parse_decimal,
        default=parse_decimal(env("BACKTEST_STARTING_CASH", "1000000")),
        help="Starting cash balance in CNY.",
    )
    parser.add_argument(
        "--trade-size",
        type=parse_decimal,
        default=parse_decimal(env("QMT_TRADE_SIZE", "100")),
        help="Order quantity. A-share lots are normally 100 shares.",
    )
    parser.add_argument(
        "--fast-ema-period",
        type=int,
        default=int(env("QMT_FAST_EMA_PERIOD", "10")),
        help="Fast EMA period.",
    )
    parser.add_argument(
        "--slow-ema-period",
        type=int,
        default=int(env("QMT_SLOW_EMA_PERIOD", "20")),
        help="Slow EMA period.",
    )
    parser.add_argument(
        "--allow-short",
        action="store_true",
        default=env("QMT_ALLOW_SHORT", "0").lower() in {"1", "true", "yes", "on"},
        help="Allow bearish signals to open short positions after flat.",
    )
    parser.add_argument(
        "--flatten-on-stop",
        action="store_true",
        default=env("QMT_FLATTEN_ON_STOP", "0").lower() in {"1", "true", "yes", "on"},
        help="Submit flattening orders when the strategy stops.",
    )
    parser.add_argument(
        "--trader-id",
        default=env("BACKTEST_TRADER_ID", "BACKTESTER-001"),
        help="Nautilus trader ID in NAME-000 format.",
    )
    parser.add_argument(
        "--log-level",
        default=env("BACKTEST_LOG_LEVEL", "INFO"),
        help="Nautilus console log level.",
    )
    parser.add_argument(
        "--strict-data",
        action="store_true",
        help="Raise on the first invalid ClickHouse row instead of skipping it.",
    )
    parser.add_argument(
        "--print-query",
        action="store_true",
        help="Print the ClickHouse query and exit without connecting.",
    )
    parser.add_argument(
        "--load-only",
        action="store_true",
        help="Load and convert bars, then exit before building/running the backtest.",
    )
    parser.add_argument(
        "--skip-reports",
        action="store_true",
        help="Do not print Nautilus account/fill/position reports after the run.",
    )
    add_benchmark_args(parser)
    REPORT_PROCESSOR.add_tearsheet_args(parser)

    args = parser.parse_args()
    if not args.start:
        parser.error("--start is required, or set BACKTEST_START")
    if not args.end:
        parser.error("--end is required, or set BACKTEST_END")
    return args


def build_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
    return REPORT_PROCESSOR.build_clickhouse_connection(args)


def build_provider(args: argparse.Namespace) -> BarDataProvider:
    return ClickHouseBarDataProvider(
        connection=build_connection(args),
        schema=ClickHouseBarSchema(),
    )


def qmt_symbol(value: str) -> str:
    return REPORT_PROCESSOR.qmt_symbol(value)


def data_symbol(value: str) -> str:
    return REPORT_PROCESSOR.data_symbol(value)


def build_bar_type(args: argparse.Namespace):
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.model.data import BarType

    instrument_id = qmt_symbol_to_instrument_id(qmt_symbol(args.symbol))
    return BarType.from_str(f"{instrument_id}-1-DAY-LAST-EXTERNAL")


def prepare_bar_data(args: argparse.Namespace) -> PreparedBarData:
    provider = build_provider(args)
    return provider.prepare_bars(
        symbol=data_symbol(args.symbol),
        bar_type=build_bar_type(args),
        start=args.start,
        end=args.end,
        timezone_name=args.exchange_timezone,
        price_precision=args.price_precision,
        strict_data=args.strict_data,
        limit=args.limit,
    )


def build_instrument(args: argparse.Namespace, ts_init: int):
    from nautilus_trader.adapters.qmt.common import parse_equity

    return parse_equity(
        symbol=qmt_symbol(args.symbol),
        fields={"name": qmt_symbol(args.symbol), "source": "clickhouse"},
        ts_event=ts_init,
        ts_init=ts_init,
    )


def build_engine(args: argparse.Namespace, prepared: PreparedBarData) -> Any:
    from nautilus_trader.adapters.qmt.constants import QMT_VENUE
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import RiskEngineConfig
    from nautilus_trader.model.currencies import CNY
    from nautilus_trader.model.enums import AccountType
    from nautilus_trader.model.enums import OmsType
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.model.objects import Money

    from strategies.emac_cross import EMACross
    from strategies.emac_cross import EMACrossConfig

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(args.trader_id),
            logging=LoggingConfig(log_level=args.log_level),
            risk_engine=RiskEngineConfig(bypass=True),
        ),
    )
    engine.add_venue(
        venue=QMT_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=CNY,
        starting_balances=[Money(args.starting_cash, CNY)],
        default_leverage=Decimal(1),
        bar_execution=True,
    )

    instrument = build_instrument(
        args, prepared.bars[0].ts_init if prepared.bars else 0
    )

    engine.add_instrument(instrument)
    engine.add_data(prepared.bars)
    engine.add_strategy(
        EMACross(
            config=EMACrossConfig(
                instrument_id=instrument.id,
                bar_type=prepared.bar_type,
                trade_size=args.trade_size,
                fast_ema_period=args.fast_ema_period,
                slow_ema_period=args.slow_ema_period,
                allow_short=args.allow_short,
                flatten_on_stop=args.flatten_on_stop,
            ),
        ),
    )
    return engine


def print_reports(engine: Any) -> None:
    REPORT_PROCESSOR.print_raw_engine_reports(engine)


def main() -> None:
    args = parse_args()
    provider = build_provider(args)
    if args.print_query:
        preview = provider.preview_request(
            symbol=data_symbol(args.symbol),
            start=args.start,
            end=args.end,
            limit=args.limit,
        )
        print(preview or "Provider does not expose a request preview.")
        return

    prepared = provider.prepare_bars(
        symbol=data_symbol(args.symbol),
        bar_type=build_bar_type(args),
        start=args.start,
        end=args.end,
        timezone_name=args.exchange_timezone,
        price_precision=args.price_precision,
        strict_data=args.strict_data,
        limit=args.limit,
    )
    print(
        f"Loaded {len(prepared.bars)} {prepared.bar_type} bars from ClickHouse; "
        f"skipped {prepared.skipped_rows} invalid rows."
    )
    if not prepared.bars:
        raise SystemExit(
            "No valid bars loaded; check the ClickHouse query, symbol, and time range."
        )
    if args.load_only:
        return

    engine = build_engine(args, prepared)
    try:
        engine.run()
        benchmark = None
        benchmark_config = benchmark_config_from_args(args)
        should_load_benchmark = benchmark_config.enabled and (
            not args.skip_reports or bool(str(getattr(args, "tearsheet_path", "") or "").strip())
        )
        if should_load_benchmark:
            benchmark = load_benchmark_returns(
                connection=build_connection(args),
                config=benchmark_config,
                start=args.start,
                end=args.end,
            )
        tearsheet_path = REPORT_PROCESSOR.write_tearsheet(
            args,
            engine,
            {"benchmark": benchmark} if benchmark is not None else {},
        )
        if tearsheet_path:
            print(f"tearsheet written: {tearsheet_path}")
        if not args.skip_reports:
            print_reports(engine)
            if benchmark is not None:
                print("\nBenchmark report")
                print(REPORT_PROCESSOR.format_report_frame(benchmark.tail(20)).to_string(index=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
