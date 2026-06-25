#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from backtests.data_providers import ClickHouseBarDataProvider  # noqa: E402
from backtests.data_providers import ClickHouseBarSchema  # noqa: E402
from backtests.data_providers import ClickHouseConnectionConfig  # noqa: E402
from backtests.base import BaseBacktest  # noqa: E402
from backtests.common import add_benchmark_args  # noqa: E402
from backtests.common import apply_benchmark_to_reports  # noqa: E402
from backtests.common import benchmark_config_from_args  # noqa: E402
from backtests.common import benchmark_run_config  # noqa: E402
from backtests.result_writers import DailyAccountRecord  # noqa: E402
from backtests.result_writers import DailyPerformanceRecord  # noqa: E402
from backtests.result_writers import DailyPositionRecord  # noqa: E402
from backtests.result_writers import ExperimentParamRecord  # noqa: E402
from backtests.result_writers import ExperimentRecord  # noqa: E402
from backtests.result_writers import MySQLResultWriter  # noqa: E402
from backtests.result_writers import OrderRecord  # noqa: E402
from backtests.result_writers import SignalRecord  # noqa: E402
from backtests.result_writers import SummaryMetricRecord  # noqa: E402
from backtests.result_writers import TargetPortfolioRecord  # noqa: E402
from backtests.result_writers import TradeRecord  # noqa: E402
from strategies.macd_smoke import MacdSmokeStrategy  # noqa: E402
from strategies.macd_smoke import MacdSmokeStrategyConfig  # noqa: E402


STRATEGY_ID = os.getenv("BACKTEST_STRATEGY_ID", "nautilus_macd_smoke")
STRATEGY_VERSION_ID = os.getenv("BACKTEST_STRATEGY_VERSION_ID", "dev")
REPORT_DECIMAL_QUANTUM = Decimal("0.001")
REPORT_PROCESSOR = BaseBacktest(REPORT_DECIMAL_QUANTUM, csv_index=False)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_args() -> argparse.Namespace:
    default_symbol = env("BACKTEST_ORDER_BOOK_ID", env("QMT_SYMBOL", "000001.XSHE"))
    parser = argparse.ArgumentParser(
        description=(
            "Run the migrated RQAlpha ClickHouse MACD smoke strategy with "
            "Nautilus and QMT venue wiring."
        ),
    )
    parser.add_argument("--symbol", default=default_symbol)
    parser.add_argument("--start", default=env("BACKTEST_START_DATE", "2024-01-02"))
    parser.add_argument("--end", default=env("BACKTEST_END_DATE", "2024-01-31"))
    parser.add_argument("--clickhouse-url", default=env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=env("CLICKHOUSE_PASSWORD"))
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(env("CLICKHOUSE_TIMEOUT_SECS", "60")),
    )
    parser.add_argument("--exchange-timezone", default=env("QMT_EXCHANGE_TIMEZONE", "Asia/Shanghai"))
    parser.add_argument("--price-precision", type=int, default=int(env("QMT_PRICE_PRECISION", "2")))
    parser.add_argument(
        "--starting-cash",
        type=parse_decimal,
        default=parse_decimal(env("BACKTEST_INIT_CASH", env("BACKTEST_STARTING_CASH", "100000"))),
    )
    parser.add_argument("--short-period", type=int, default=int(env("MACD_SHORT_PERIOD", "12")))
    parser.add_argument("--long-period", type=int, default=int(env("MACD_LONG_PERIOD", "26")))
    parser.add_argument("--signal-period", type=int, default=int(env("MACD_SIGNAL_PERIOD", "9")))
    parser.add_argument("--observation", type=int, default=None)
    parser.add_argument("--entry-ma-period", type=int, default=int(env("MACD_ENTRY_MA_PERIOD", "120")))
    parser.add_argument("--exit-ma-period", type=int, default=int(env("MACD_EXIT_MA_PERIOD", "60")))
    parser.add_argument("--target-percent", type=float, default=float(env("MACD_TARGET_PERCENT", "0.6")))
    parser.add_argument("--stop-loss", type=float, default=float(env("MACD_STOP_LOSS", "0.08")))
    parser.add_argument(
        "--warmup-days",
        type=int,
        default=int(env("BACKTEST_WARMUP_DAYS", "0")),
        help="Calendar days loaded before --start for indicator warmup. 0 chooses a MACD-aware default.",
    )
    parser.add_argument("--limit", type=int, default=int(env("CLICKHOUSE_LIMIT", "0")))
    parser.add_argument("--trader-id", default=env("BACKTEST_TRADER_ID", "BACKTESTER-001"))
    parser.add_argument("--log-level", default=env("BACKTEST_LOG_LEVEL", "INFO"))
    parser.add_argument("--strict-data", action="store_true")
    parser.add_argument("--print-query", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--report-dir", default=env("BACKTEST_REPORT_PATH"))
    parser.add_argument("--write-results", action="store_true", default=env_bool("BACKTEST_RESULT_WRITE_ENABLED", False))
    add_benchmark_args(parser)
    REPORT_PROCESSOR.add_tearsheet_args(parser)
    args = parser.parse_args()
    args.symbol = qmt_symbol(args.symbol)
    args.observation = args.observation or int(
        env(
            "MACD_OBSERVATION",
            str(max(180, args.entry_ma_period + 2, args.long_period + args.signal_period + 2)),
        ),
    )
    if args.warmup_days <= 0:
        args.warmup_days = max(365, args.observation * 2)
    return args


def qmt_symbol(value: str) -> str:
    return REPORT_PROCESSOR.qmt_symbol(value)


def data_symbol(value: str) -> str:
    return REPORT_PROCESSOR.data_symbol(value)


def build_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
    return REPORT_PROCESSOR.build_clickhouse_connection(args)


def build_bar_type(stock_code: str):
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.model.data import BarType

    instrument_id = qmt_symbol_to_instrument_id(qmt_symbol(stock_code))
    return BarType.from_str(f"{instrument_id}-1-DAY-LAST-EXTERNAL")


def load_window(args: argparse.Namespace) -> tuple[str, str]:
    warmup_start = (pd.Timestamp(args.start).normalize() - pd.Timedelta(days=args.warmup_days)).date().isoformat()
    end_exclusive = (pd.Timestamp(args.end).normalize() + pd.Timedelta(days=1)).date().isoformat()
    return warmup_start, end_exclusive


def load_bars(args: argparse.Namespace, connection: ClickHouseConnectionConfig):
    provider = ClickHouseBarDataProvider(connection=connection, schema=ClickHouseBarSchema())
    start, end = load_window(args)
    prepared = provider.prepare_bars(
        symbol=data_symbol(args.symbol),
        bar_type=build_bar_type(args.symbol),
        start=start,
        end=end,
        timezone_name=args.exchange_timezone,
        price_precision=args.price_precision,
        strict_data=args.strict_data,
        limit=args.limit,
    )
    return prepared


def build_engine(args: argparse.Namespace, prepared: Any) -> tuple[Any, MacdSmokeStrategy]:
    from nautilus_trader.adapters.qmt.common import parse_equity
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
    instrument = parse_equity(
        symbol=args.symbol,
        fields={"name": args.symbol, "source": "clickhouse"},
        ts_event=prepared.bars[0].ts_init,
        ts_init=prepared.bars[0].ts_init,
    )
    engine.add_instrument(instrument)
    engine.add_data(prepared.bars)
    strategy = MacdSmokeStrategy(
        config=MacdSmokeStrategyConfig(
            instrument_id=instrument.id,
            bar_type=prepared.bar_type,
            trade_start_date=args.start,
            trade_end_date=args.end,
            short_period=args.short_period,
            long_period=args.long_period,
            signal_period=args.signal_period,
            observation=args.observation,
            entry_ma_period=args.entry_ma_period,
            exit_ma_period=args.exit_ma_period,
            target_percent=args.target_percent,
            stop_loss=args.stop_loss,
            initial_cash=args.starting_cash,
            timezone_name=args.exchange_timezone,
        ),
    )
    engine.add_strategy(strategy)
    return engine, strategy


def build_result_writer(args: argparse.Namespace):
    if not args.write_results:
        return None
    return MySQLResultWriter.from_pymysql_kwargs(
        host=env("MYSQL_HOST", "localhost"),
        port=int(env("MYSQL_PORT", "3306")),
        user=env("MYSQL_USER", "root"),
        password=env("MYSQL_PASSWORD", ""),
        database=env("MYSQL_DATABASE", "backtest"),
        charset="utf8mb4",
    )


def experiment_id(args: argparse.Namespace) -> str:
    configured = env("BACKTEST_EXPERIMENT_ID")
    if configured:
        return configured
    return f"nautilus-macd-smoke-{args.symbol.replace('.', '-')}-{uuid.uuid4().hex[:12]}"


def create_experiment_record(args: argparse.Namespace, experiment_id_value: str, started_at: datetime) -> ExperimentRecord:
    benchmark_config = benchmark_config_from_args(args)
    return ExperimentRecord(
        experiment_id=experiment_id_value,
        experiment_name=env("BACKTEST_EXPERIMENT_NAME", "Nautilus ClickHouse MACD Smoke"),
        strategy_id=STRATEGY_ID,
        strategy_version_id=STRATEGY_VERSION_ID,
        start_date=pd.Timestamp(args.start).date(),
        end_date=pd.Timestamp(args.end).date(),
        frequency="1d",
        initial_cash=args.starting_cash,
        engine_name="nautilus_trader",
        status="running",
        benchmark=benchmark_config.display_name if benchmark_config.enabled else None,
        universe_id=args.symbol,
        cost_config={},
        slippage_config={},
        risk_config={"risk_engine_bypass": True},
        run_config=run_config(args),
        started_at=started_at,
        created_at=started_at,
    )


def experiment_params(args: argparse.Namespace, experiment_id_value: str) -> list[ExperimentParamRecord]:
    records = []
    for name, value in run_config(args).items():
        if isinstance(value, bool):
            param_type = "bool"
        elif isinstance(value, int):
            param_type = "int"
        elif isinstance(value, float):
            param_type = "float"
        elif isinstance(value, Decimal):
            param_type = "decimal"
        elif value is None:
            param_type = "null"
        else:
            param_type = "string"
        records.append(
            ExperimentParamRecord(
                experiment_id=experiment_id_value,
                param_group=name.split(".", 1)[0],
                param_name=name,
                param_value="" if value is None else str(value),
                param_type=param_type,
            ),
        )
    return records


def run_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "strategy.symbol": args.symbol,
        "strategy.short_period": args.short_period,
        "strategy.long_period": args.long_period,
        "strategy.signal_period": args.signal_period,
        "strategy.observation": args.observation,
        "strategy.entry_ma_period": args.entry_ma_period,
        "strategy.exit_ma_period": args.exit_ma_period,
        "strategy.target_percent": args.target_percent,
        "strategy.stop_loss": args.stop_loss,
        "data.table": "dws_stock_factor_wide",
        "data.symbol": data_symbol(args.symbol),
        "data.warmup_days": args.warmup_days,
        "base.start_date": args.start,
        "base.end_date": args.end,
        "base.initial_cash": args.starting_cash,
        **benchmark_run_config(args),
        **REPORT_PROCESSOR.tearsheet_run_config(args),
    }


def build_complete_report(
    args: argparse.Namespace,
    engine: Any,
    strategy: MacdSmokeStrategy,
    bars: list[Any],
) -> dict[str, pd.DataFrame]:
    raw_reports = REPORT_PROCESSOR.raw_engine_reports(engine)
    fills_report = raw_reports["fills"]
    daily_portfolio, daily_positions = reconstruct_daily_portfolio(args, strategy, fills_report, bars)
    return REPORT_PROCESSOR.complete_report(
        engine=engine,
        daily_portfolio=daily_portfolio,
        daily_positions=daily_positions,
        raw_reports=raw_reports,
        extra_reports={
            "signals": strategy_signal_frame(strategy),
            "targets": strategy_target_frame(strategy),
            "strategy_orders": strategy_order_frame(strategy),
        },
    )


def reconstruct_daily_portfolio(
    args: argparse.Namespace,
    strategy: MacdSmokeStrategy,
    fills_report: Any,
    bars: list[Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    close_by_date, dates = close_prices_by_date(args, bars)
    dates = [value for value in dates if pd.Timestamp(args.start) <= value <= pd.Timestamp(args.end)]
    fills_by_date = fills_by_exchange_date(args, fills_report)
    quantity = Decimal("0")
    avg_cost = Decimal("0")
    cash = Decimal(str(args.starting_cash))
    initial_cash = Decimal(str(args.starting_cash)) or Decimal("1")
    last_price = Decimal("0")
    previous_total: Decimal | None = None
    high_water = Decimal("0")
    portfolio_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []

    for trading_date in dates:
        last_price = close_by_date.get(trading_date, last_price)
        for fill in fills_by_date.get(trading_date, []):
            fill_qty = fill["quantity"]
            fill_price = fill["price"]
            amount = fill_qty * fill_price
            if fill["side"] == "buy":
                new_qty = quantity + fill_qty
                previous_cost = avg_cost * quantity
                avg_cost = (previous_cost + amount) / new_qty if new_qty > 0 else Decimal("0")
                quantity = new_qty
                cash -= amount
            elif fill["side"] == "sell":
                sell_qty = min(quantity, fill_qty)
                quantity -= sell_qty
                cash += sell_qty * fill_price
                if quantity <= 0:
                    quantity = Decimal("0")
                    avg_cost = Decimal("0")

        market_value = quantity * last_price if quantity > 0 else Decimal("0")
        unrealized_pnl = quantity * (last_price - avg_cost) if quantity > 0 else Decimal("0")
        total_equity = cash + market_value
        high_water = max(high_water, total_equity)
        daily_return = Decimal("0") if previous_total in (None, Decimal("0")) else total_equity / previous_total - 1
        net_value = total_equity / initial_cash
        cum_return = net_value - 1
        drawdown = Decimal("0") if high_water == 0 else total_equity / high_water - 1
        active_positions = 1 if quantity > 0 else 0
        if quantity > 0:
            weight = Decimal("0") if total_equity == 0 else market_value / total_equity
            position_rows.append(
                {
                    "date": trading_date.date(),
                    "instrument_id": str(strategy.config.instrument_id),
                    "stock_code": args.symbol,
                    "quantity": int(quantity),
                    "avg_cost": avg_cost,
                    "last_price": last_price,
                    "market_value": market_value,
                    "weight": weight,
                    "unrealized_pnl": unrealized_pnl,
                },
            )
        portfolio_rows.append(
            {
                "date": trading_date.date(),
                "cash": cash,
                "market_value": market_value,
                "total_equity": total_equity,
                "net_value": net_value,
                "daily_return": daily_return,
                "cum_return": cum_return,
                "drawdown": drawdown,
                "unrealized_pnl": unrealized_pnl,
                "active_positions": active_positions,
            },
        )
        previous_total = total_equity

    return pd.DataFrame(portfolio_rows), pd.DataFrame(position_rows)


def daily_cash_ledger(daily_portfolio: pd.DataFrame) -> pd.DataFrame:
    return REPORT_PROCESSOR.daily_cash_ledger(daily_portfolio)


def close_prices_by_date(args: argparse.Namespace, bars: list[Any]) -> tuple[dict[pd.Timestamp, Decimal], list[pd.Timestamp]]:
    closes: dict[pd.Timestamp, Decimal] = {}
    for bar in bars:
        trading_date = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(args.exchange_timezone).date()
        closes[pd.Timestamp(trading_date)] = Decimal(str(float(bar.close)))
    return closes, sorted(closes)


def fills_by_exchange_date(args: argparse.Namespace, fills_report: Any) -> dict[pd.Timestamp, list[dict[str, Any]]]:
    if not isinstance(fills_report, pd.DataFrame) or fills_report.empty:
        return {}
    result: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    for _, row in fills_report.reset_index().iterrows():
        event_time = timestamp_value(first_value(row, "ts_init", "ts_last", "time", "datetime", default=None))
        if event_time is None:
            continue
        timestamp = pd.Timestamp(event_time)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        trading_date = pd.Timestamp(timestamp.tz_convert(args.exchange_timezone).date())
        side = side_text(first_value(row, "side", "order_side", default=""))
        quantity = Decimal(str(int_or_zero(first_value(row, "filled_qty", "quantity", "qty", default=0))))
        price = decimal_or_zero(first_value(row, "avg_px", "last_px", "price", default=0))
        if quantity <= 0 or price <= 0:
            continue
        result.setdefault(trading_date, []).append(
            {
                "side": side,
                "quantity": quantity,
                "price": price,
            },
        )
    return result


def strategy_signal_frame(strategy: MacdSmokeStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_date": event.signal_date,
                "instrument_id": event.instrument_id,
                "signal_name": event.signal_name,
                "signal_value": event.signal_value,
                "score": event.score,
                "selected": event.selected,
                "reason": event.reason,
                "extra": event.extra,
            }
            for event in strategy.signal_events
        ],
    )


def strategy_target_frame(strategy: MacdSmokeStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "target_id": event.target_id,
                "target_date": event.target_date,
                "execute_date": event.execute_date,
                "instrument_id": event.instrument_id,
                "target_weight": event.target_weight,
                "current_weight": event.current_weight,
                "delta_weight": event.delta_weight,
                "reason": event.reason,
                "extra": event.extra,
            }
            for event in strategy.target_events
        ],
    )


def strategy_order_frame(strategy: MacdSmokeStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "order_id": event.order_id,
                "trading_date": event.trading_date,
                "instrument_id": event.instrument_id,
                "side": event.side,
                "quantity": event.quantity,
                "target_weight": event.target_weight,
                "status": event.status,
                "reason": event.reason,
                "extra": event.extra,
            }
            for event in strategy.order_events
        ],
    )


def write_result_records(
    writer: Any,
    experiment_id_value: str,
    engine: Any,
    strategy: MacdSmokeStrategy,
    complete_report: dict[str, pd.DataFrame],
) -> None:
    writer.write_signals(signal_records(experiment_id_value, strategy))
    writer.write_target_portfolios(target_records(experiment_id_value, strategy))
    writer.write_orders(order_records(experiment_id_value, strategy))
    writer.write_trades(trade_records_from_report(experiment_id_value, engine.trader.generate_order_fills_report()))
    portfolio_report = complete_report.get("daily_portfolio", pd.DataFrame())
    writer.write_daily_accounts(daily_account_records(experiment_id_value, portfolio_report))
    writer.write_daily_performance(daily_performance_records(experiment_id_value, portfolio_report))
    writer.write_daily_positions(daily_position_records(experiment_id_value, complete_report.get("daily_positions", pd.DataFrame())))
    writer.write_summary_metrics(summary_metric_records(experiment_id_value, strategy, portfolio_report))


def signal_records(experiment_id_value: str, strategy: MacdSmokeStrategy) -> list[SignalRecord]:
    return [
        SignalRecord(
            experiment_id=experiment_id_value,
            signal_date=event.signal_date,
            instrument_id=event.instrument_id,
            signal_name=event.signal_name,
            signal_value=decimal_or_none(event.signal_value),
            score=decimal_or_none(event.score),
            selected=event.selected,
            reason=event.reason,
            extra=event.extra,
        )
        for event in strategy.signal_events
    ]


def target_records(experiment_id_value: str, strategy: MacdSmokeStrategy) -> list[TargetPortfolioRecord]:
    return [
        TargetPortfolioRecord(
            experiment_id=experiment_id_value,
            target_id=event.target_id,
            target_date=event.target_date,
            execute_date=event.execute_date,
            instrument_id=event.instrument_id,
            target_weight=decimal_or_none(event.target_weight),
            current_weight=decimal_or_none(event.current_weight),
            delta_weight=decimal_or_none(event.delta_weight),
            source_signal_name="macd_target_percent",
            reason=event.reason,
            extra=event.extra,
        )
        for event in strategy.target_events
    ]


def order_records(experiment_id_value: str, strategy: MacdSmokeStrategy) -> list[OrderRecord]:
    return [
        OrderRecord(
            experiment_id=experiment_id_value,
            order_id=event.order_id,
            trading_date=event.trading_date,
            submit_time=datetime.combine(event.trading_date, datetime.min.time()),
            instrument_id=event.instrument_id,
            side=event.side,
            order_type="target_weight",
            price_type="market",
            quantity=event.quantity,
            target_weight=decimal_or_none(event.target_weight),
            status=event.status,
            rejected_reason=event.reason if event.status == "rejected" else None,
            extra=event.extra,
        )
        for event in strategy.order_events
    ]


def trade_records_from_report(experiment_id_value: str, frame: Any) -> list[TradeRecord]:
    return REPORT_PROCESSOR.trade_records_from_report(experiment_id_value, frame)


def daily_account_records(experiment_id_value: str, frame: Any) -> list[DailyAccountRecord]:
    return REPORT_PROCESSOR.daily_account_records(experiment_id_value, frame)


def daily_performance_records(experiment_id_value: str, frame: Any) -> list[DailyPerformanceRecord]:
    return REPORT_PROCESSOR.daily_performance_records(experiment_id_value, frame)


def daily_position_records(experiment_id_value: str, frame: Any) -> list[DailyPositionRecord]:
    return REPORT_PROCESSOR.daily_position_records(experiment_id_value, frame)


def summary_metric_records(
    experiment_id_value: str,
    strategy: MacdSmokeStrategy,
    portfolio_report: Any,
) -> list[SummaryMetricRecord]:
    return REPORT_PROCESSOR.summary_metric_records(
        experiment_id_value=experiment_id_value,
        count_metrics={
            "signal_count": len(strategy.signal_events),
            "target_count": len(strategy.target_events),
            "order_count": len(strategy.order_events),
        },
        portfolio_report=portfolio_report,
    )


def write_report_dir(report_dir: str, reports: dict[str, pd.DataFrame]) -> None:
    REPORT_PROCESSOR.write_report_dir(report_dir, reports)


def print_reports(engine: Any, complete_report: dict[str, pd.DataFrame]) -> None:
    REPORT_PROCESSOR.print_complete_report(engine, complete_report)


def format_report_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return REPORT_PROCESSOR.format_report_frame(frame)


def format_report_value(value: Any) -> Any:
    return REPORT_PROCESSOR.format_report_value(value)


def quantize_report_decimal(value: Decimal) -> Decimal:
    return REPORT_PROCESSOR.quantize_report_decimal(value)


def safe_report(callback):
    return REPORT_PROCESSOR.safe_report(callback)


def decimal_or_none(value: Any) -> Decimal | None:
    return REPORT_PROCESSOR.decimal_or_none(value)


def decimal_or_zero(value: Any) -> Decimal:
    return REPORT_PROCESSOR.decimal_or_zero(value)


def int_or_zero(value: Any) -> int:
    return REPORT_PROCESSOR.int_or_zero(value)


def first_value(row: pd.Series, *names: str, default: Any = None) -> Any:
    return REPORT_PROCESSOR.first_value(row, *names, default=default)


def timestamp_value(value: Any) -> datetime | None:
    return REPORT_PROCESSOR.timestamp_value(value)


def side_text(value: Any) -> str:
    return REPORT_PROCESSOR.side_text(value)


def main() -> None:
    args = parse_args()
    connection = build_connection(args)
    provider = ClickHouseBarDataProvider(connection=connection, schema=ClickHouseBarSchema())
    load_start, load_end = load_window(args)
    if args.print_query:
        print(
            provider.preview_request(
                symbol=data_symbol(args.symbol),
                start=load_start,
                end=load_end,
                limit=args.limit,
            ),
        )
        return

    prepared = load_bars(args, connection)
    print(
        f"Loaded {len(prepared.bars)} {prepared.bar_type} bars from ClickHouse; "
        f"skipped {prepared.skipped_rows} invalid rows. "
        f"trade_window={args.start}..{args.end} load_window={load_start}..{load_end}"
    )
    if not prepared.bars:
        raise SystemExit("No valid bars loaded; check the ClickHouse query, symbol, and time range.")
    if args.load_only:
        return

    writer = build_result_writer(args)
    experiment_id_value = experiment_id(args)
    started_at = datetime.now()
    if writer is not None:
        writer.create_experiment(create_experiment_record(args, experiment_id_value, started_at))
        writer.write_experiment_params(experiment_params(args, experiment_id_value))

    engine, strategy = build_engine(args, prepared)
    try:
        engine.run()
        complete_report = build_complete_report(args, engine, strategy, prepared.bars)
        complete_report = apply_benchmark_to_reports(args, connection, complete_report)
        tearsheet_path = REPORT_PROCESSOR.write_tearsheet(args, engine, complete_report)
        if tearsheet_path:
            print(f"tearsheet written: {tearsheet_path}")
        if not args.skip_reports:
            print_reports(engine, complete_report)
        if args.report_dir:
            write_report_dir(args.report_dir, complete_report)
            print(f"complete report written: {args.report_dir}")
        print(
            "MACD smoke summary: "
            f"signals={len(strategy.signal_events)} "
            f"targets={len(strategy.target_events)} "
            f"orders={len(strategy.order_events)}"
        )
        if writer is not None:
            write_result_records(writer, experiment_id_value, engine, strategy, complete_report)
            writer.finalize_experiment(experiment_id_value, finished_at=datetime.now())
            print(f"result persisted: experiment_id={experiment_id_value}")
    except Exception as exc:
        if writer is not None:
            writer.fail_experiment(experiment_id_value, str(exc), finished_at=datetime.now())
        raise
    finally:
        if writer is not None:
            writer.close()
        engine.dispose()


if __name__ == "__main__":
    main()
