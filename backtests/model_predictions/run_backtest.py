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
from backtests.data_providers import ClickHouseModelPredictionDataProvider  # noqa: E402
from backtests.data_providers import ModelPredictionDataRequest  # noqa: E402
from backtests.data_providers import PredictionDataBundle  # noqa: E402
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
from strategies.model_predictions import ModelPredictionsStrategy  # noqa: E402
from strategies.model_predictions import ModelPredictionsStrategyConfig  # noqa: E402


STRATEGY_ID = os.getenv("BACKTEST_STRATEGY_ID", "nautilus_model_predictions")
STRATEGY_VERSION_ID = os.getenv("BACKTEST_STRATEGY_VERSION_ID", "dev")
REPORT_DECIMAL_QUANTUM = Decimal("0.001")
REPORT_PROCESSOR = BaseBacktest(REPORT_DECIMAL_QUANTUM, csv_index=True)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in (env(name, default) or "").split(",") if item.strip()]


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the migrated model-prediction strategy with Nautilus and QMT venue wiring.",
    )
    parser.add_argument("--start", default=env("BACKTEST_START_DATE", "2025-01-02"))
    parser.add_argument("--end", default=env("BACKTEST_END_DATE", "2025-12-31"))
    parser.add_argument("--predictions-table", default=env("MODEL_PREDICTIONS_TABLE", "model_predictions"))
    parser.add_argument("--stock-codes", default=",".join(env_list("MODEL_STOCK_CODES", "000001.SZ,000002.SZ")))
    parser.add_argument("--all-stocks", action="store_true", default=env_bool("MODEL_ALL_STOCKS", False))
    parser.add_argument("--excluded-stock-codes", default=",".join(env_list("MODEL_EXCLUDED_STOCK_CODES", "")))
    parser.add_argument("--min-score", type=float, default=parse_optional_float(env("MODEL_MIN_SCORE")))
    parser.add_argument("--top-frac", type=float, default=float(env("MODEL_TOP_FRAC", "0.10")))
    parser.add_argument("--max-positions", type=int, default=int(env("MODEL_MAX_POSITIONS", "50")))
    parser.add_argument(
        "--price-offset-ticks",
        type=int,
        default=int(env("MODEL_PRICE_OFFSET_TICKS", "1")),
        help="Limit-order offset in ticks past the touch: buy at ask+N*tick, sell at bid-N*tick.",
    )
    parser.add_argument(
        "--max-position-percent",
        type=float,
        default=float(env("MODEL_MAX_POSITION_PERCENT", "0.03")),
    )
    parser.add_argument("--holding-days", type=int, default=int(env("MODEL_HOLDING_DAYS", "10")))
    parser.add_argument("--stop-loss", type=float, default=float(env("MODEL_STOP_LOSS", "0.05")))
    parser.add_argument(
        "--trailing-take-profit",
        type=float,
        default=float(env("MODEL_TRAILING_TAKE_PROFIT", "0.0")),
    )
    parser.add_argument(
        "--trailing-take-profit-start",
        type=float,
        default=float(env("MODEL_TRAILING_TAKE_PROFIT_START", "0.0")),
    )
    parser.add_argument("--min-listed-days", type=int, default=int(env("MODEL_MIN_LISTED_DAYS", "120")))
    parser.add_argument("--signal-warmup-days", type=int, default=int(env("MODEL_SIGNAL_WARMUP_DAYS", "7")))
    parser.add_argument("--max-universe", type=int, default=int(env("MODEL_MAX_UNIVERSE", "0")))
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
        default=parse_decimal(env("BACKTEST_INIT_CASH", env("BACKTEST_STARTING_CASH", "1000000"))),
    )
    parser.add_argument("--trader-id", default=env("BACKTEST_TRADER_ID", "BACKTESTER-001"))
    parser.add_argument("--log-level", default=env("BACKTEST_LOG_LEVEL", "INFO"))
    parser.add_argument("--strict-data", action="store_true")
    parser.add_argument("--print-signals", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--report-dir", default=env("BACKTEST_REPORT_PATH"))
    parser.add_argument("--write-results", action="store_true", default=env_bool("BACKTEST_RESULT_WRITE_ENABLED", False))
    add_benchmark_args(parser)
    REPORT_PROCESSOR.add_tearsheet_args(parser)
    return parser.parse_args()


def build_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
    return REPORT_PROCESSOR.build_clickhouse_connection(args)


def build_prediction_request(args: argparse.Namespace) -> ModelPredictionDataRequest:
    return ModelPredictionDataRequest(
        start_date=args.start,
        end_date=args.end,
        predictions_table=args.predictions_table,
        stock_codes=env_list_from_value(args.stock_codes),
        all_stocks=args.all_stocks,
        excluded_stock_codes=set(env_list_from_value(args.excluded_stock_codes)),
        min_score=args.min_score,
        top_frac=args.top_frac,
        max_positions=args.max_positions,
        signal_warmup_days=args.signal_warmup_days,
    )


def env_list_from_value(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def qmt_symbol(stock_code: str) -> str:
    return REPORT_PROCESSOR.qmt_symbol(stock_code)


def data_symbol(stock_code: str) -> str:
    return REPORT_PROCESSOR.data_symbol(stock_code)


def build_bar_type(stock_code: str):
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.model.data import BarType

    instrument_id = qmt_symbol_to_instrument_id(qmt_symbol(stock_code))
    return BarType.from_str(f"{instrument_id}-1-DAY-LAST-EXTERNAL")


def load_bars(
    args: argparse.Namespace,
    connection: ClickHouseConnectionConfig,
    bundle: PredictionDataBundle,
) -> tuple[dict[str, Any], dict[str, list[Any]], int]:
    provider = ClickHouseBarDataProvider(connection=connection, schema=ClickHouseBarSchema())
    end_exclusive = (pd.Timestamp(args.end).normalize() + pd.Timedelta(days=1)).date().isoformat()
    stock_codes = list(bundle.universe)
    if args.max_universe > 0:
        stock_codes = stock_codes[: args.max_universe]

    bar_types: dict[str, Any] = {}
    bars_by_stock: dict[str, list[Any]] = {}
    skipped_rows = 0
    for stock_code in stock_codes:
        bar_type = build_bar_type(stock_code)
        prepared = provider.prepare_bars(
            symbol=data_symbol(stock_code),
            bar_type=bar_type,
            start=args.start,
            end=end_exclusive,
            timezone_name=args.exchange_timezone,
            price_precision=args.price_precision,
            strict_data=args.strict_data,
        )
        skipped_rows += prepared.skipped_rows
        if prepared.bars:
            bar_types[stock_code] = prepared.bar_type
            bars_by_stock[stock_code] = prepared.bars
    return bar_types, bars_by_stock, skipped_rows


def build_engine(
    args: argparse.Namespace,
    bundle: PredictionDataBundle,
    bar_types: dict[str, Any],
    bars_by_stock: dict[str, list[Any]],
) -> tuple[Any, ModelPredictionsStrategy]:
    from nautilus_trader.adapters.qmt.common import parse_equity
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
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

    loaded_stock_codes = sorted(bars_by_stock)
    instrument_ids = []
    config_bar_types = {}
    instrument_stock_codes = {}
    all_bars = []
    for stock_code in loaded_stock_codes:
        bars = bars_by_stock[stock_code]
        instrument = parse_equity(
            symbol=qmt_symbol(stock_code),
            fields={
                "name": bundle.instrument_names.get(stock_code, stock_code),
                "source": "clickhouse",
            },
            ts_event=bars[0].ts_init,
            ts_init=bars[0].ts_init,
        )
        engine.add_instrument(instrument)
        instrument_id = qmt_symbol_to_instrument_id(qmt_symbol(stock_code))
        instrument_ids.append(instrument_id)
        config_bar_types[str(instrument_id)] = bar_types[stock_code]
        instrument_stock_codes[str(instrument_id)] = stock_code
        all_bars.extend(bars)

    engine.add_data(sorted(all_bars, key=lambda bar: bar.ts_init))
    strategy = ModelPredictionsStrategy(
        config=ModelPredictionsStrategyConfig(
            instrument_ids=instrument_ids,
            bar_types=config_bar_types,
            instrument_stock_codes=instrument_stock_codes,
            signals_by_date=signals_config(bundle, set(loaded_stock_codes)),
            trading_dates=[value.isoformat() for value in bundle.trading_dates],
            listed_dates={key: value.isoformat() for key, value in bundle.listed_dates.items()},
            st_by_date={key.isoformat(): sorted(values) for key, values in bundle.st_by_date.items()},
            suspended_by_date={
                key.isoformat(): sorted(values)
                for key, values in bundle.suspended_by_date.items()
            },
            max_positions=args.max_positions,
            max_position_percent=args.max_position_percent,
            holding_days=args.holding_days,
            stop_loss=args.stop_loss,
            trailing_take_profit=args.trailing_take_profit,
            trailing_take_profit_start=args.trailing_take_profit_start,
            min_listed_days=args.min_listed_days,
            initial_cash=args.starting_cash,
            timezone_name=args.exchange_timezone,
            price_offset_ticks=args.price_offset_ticks,
        ),
    )
    engine.add_strategy(strategy)
    return engine, strategy


def signals_config(bundle: PredictionDataBundle, loaded_stock_codes: set[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for signal_date, signals in bundle.signals_by_date.items():
        rows = []
        for signal in signals:
            if signal.stock_code not in loaded_stock_codes:
                continue
            rows.append(
                {
                    "date": signal.signal_date.isoformat(),
                    "stock_code": signal.stock_code,
                    "score": signal.score,
                    "rank": signal.rank,
                    "pred_return_live": signal.pred_return_live,
                },
            )
        if rows:
            result[signal_date.isoformat()] = rows
    return result


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


def experiment_id() -> str:
    return env("BACKTEST_EXPERIMENT_ID") or f"nautilus-model-predictions-{uuid.uuid4().hex[:12]}"


def create_experiment_record(args: argparse.Namespace, experiment_id_value: str, started_at: datetime) -> ExperimentRecord:
    benchmark_config = benchmark_config_from_args(args)
    return ExperimentRecord(
        experiment_id=experiment_id_value,
        experiment_name=env("BACKTEST_EXPERIMENT_NAME", "Nautilus Model Predictions"),
        strategy_id=STRATEGY_ID,
        strategy_version_id=STRATEGY_VERSION_ID,
        start_date=pd.Timestamp(args.start).date(),
        end_date=pd.Timestamp(args.end).date(),
        frequency="1d",
        initial_cash=args.starting_cash,
        engine_name="nautilus_trader",
        status="running",
        benchmark=benchmark_config.display_name if benchmark_config.enabled else None,
        universe_id=None,
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
        "strategy.predictions_table": args.predictions_table,
        "strategy.stock_codes": args.stock_codes,
        "strategy.all_stocks": args.all_stocks,
        "strategy.excluded_stock_codes": args.excluded_stock_codes,
        "strategy.min_score": args.min_score,
        "strategy.top_frac": args.top_frac,
        "strategy.max_positions": args.max_positions,
        "strategy.max_position_percent": args.max_position_percent,
        "strategy.holding_days": args.holding_days,
        "strategy.stop_loss": args.stop_loss,
        "strategy.trailing_take_profit": args.trailing_take_profit,
        "strategy.trailing_take_profit_start": args.trailing_take_profit_start,
        "strategy.min_listed_days": args.min_listed_days,
        "base.start_date": args.start,
        "base.end_date": args.end,
        "base.initial_cash": args.starting_cash,
        **benchmark_run_config(args),
        **REPORT_PROCESSOR.tearsheet_run_config(args),
    }


def write_result_records(
    writer: Any,
    experiment_id_value: str,
    engine: Any,
    strategy: ModelPredictionsStrategy,
    complete_report: dict[str, pd.DataFrame],
) -> None:
    from nautilus_trader.adapters.qmt.constants import QMT_VENUE

    writer.write_signals(signal_records(experiment_id_value, strategy))
    writer.write_target_portfolios(target_records(experiment_id_value, strategy))
    writer.write_orders(order_records(experiment_id_value, strategy))
    writer.write_trades(trade_records_from_report(experiment_id_value, engine.trader.generate_order_fills_report()))
    account_report = safe_report(lambda: engine.trader.generate_account_report(QMT_VENUE))
    portfolio_report = complete_report.get("daily_portfolio", pd.DataFrame())
    writer.write_daily_accounts(daily_account_records(experiment_id_value, portfolio_report))
    writer.write_daily_performance(daily_performance_records(experiment_id_value, portfolio_report))
    writer.write_daily_positions(
        daily_position_records(experiment_id_value, complete_report.get("daily_positions", pd.DataFrame())),
    )
    writer.write_summary_metrics(summary_metric_records(experiment_id_value, strategy, account_report, portfolio_report))


def signal_records(experiment_id_value: str, strategy: ModelPredictionsStrategy) -> list[SignalRecord]:
    return [
        SignalRecord(
            experiment_id=experiment_id_value,
            signal_date=event.signal_date,
            instrument_id=event.instrument_id,
            signal_name=event.signal_name,
            score=decimal_or_none(event.score),
            signal_rank=event.rank,
            selected=event.selected,
            reason=event.extra.get("reason"),
            extra={
                **event.extra,
                "side": event.side,
                "stock_code": event.stock_code,
            },
        )
        for event in strategy.signal_events
    ]


def target_records(experiment_id_value: str, strategy: ModelPredictionsStrategy) -> list[TargetPortfolioRecord]:
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
            source_signal_name="model_prediction_score",
            reason=event.reason,
            extra=event.extra,
        )
        for event in strategy.target_events
    ]


def order_records(experiment_id_value: str, strategy: ModelPredictionsStrategy) -> list[OrderRecord]:
    records = []
    for event in strategy.order_events:
        records.append(
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
            ),
        )
    return records


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
    strategy: ModelPredictionsStrategy,
    account_report: Any,
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
        account_report=account_report,
    )


def build_complete_report(
    args: argparse.Namespace,
    engine: Any,
    strategy: ModelPredictionsStrategy,
    bars_by_stock: dict[str, list[Any]],
) -> dict[str, pd.DataFrame]:
    raw_reports = REPORT_PROCESSOR.raw_engine_reports(engine)
    fills_report = raw_reports["fills"]
    daily_portfolio, daily_positions = reconstruct_daily_portfolio(
        args=args,
        strategy=strategy,
        fills_report=fills_report,
        bars_by_stock=bars_by_stock,
    )
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
    strategy: ModelPredictionsStrategy,
    fills_report: Any,
    bars_by_stock: dict[str, list[Any]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    close_by_date, dates = close_prices_by_date(args, bars_by_stock)
    fills_by_date = fills_by_exchange_date(args, fills_report, strategy)
    positions: dict[str, Decimal] = {}
    avg_costs: dict[str, Decimal] = {}
    last_prices: dict[str, Decimal] = {}
    cash = Decimal(str(args.starting_cash))
    initial_cash = Decimal(str(args.starting_cash)) or Decimal("1")
    previous_total: Decimal | None = None
    high_water = Decimal("0")
    portfolio_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []

    for trading_date in dates:
        for stock_code, close_price in close_by_date.get(trading_date, {}).items():
            last_prices[stock_code] = close_price

        for fill in fills_by_date.get(trading_date, []):
            stock_code = fill["stock_code"]
            quantity = fill["quantity"]
            price = fill["price"]
            amount = quantity * price
            current_qty = positions.get(stock_code, Decimal("0"))
            if fill["side"] == "buy":
                cash -= amount
                new_qty = current_qty + quantity
                if new_qty > 0:
                    previous_cost = avg_costs.get(stock_code, Decimal("0")) * current_qty
                    avg_costs[stock_code] = (previous_cost + amount) / new_qty
                positions[stock_code] = new_qty
            elif fill["side"] == "sell":
                cash += amount
                new_qty = current_qty - quantity
                positions[stock_code] = max(new_qty, Decimal("0"))
                if positions[stock_code] == 0:
                    avg_costs.pop(stock_code, None)

        market_value = Decimal("0")
        unrealized_pnl = Decimal("0")
        active_count = 0
        daily_position_rows: list[dict[str, Any]] = []
        for stock_code, quantity in sorted(positions.items()):
            if quantity <= 0:
                continue
            last_price = last_prices.get(stock_code)
            if last_price is None:
                continue
            avg_cost = avg_costs.get(stock_code, Decimal("0"))
            value = quantity * last_price
            pnl = quantity * (last_price - avg_cost)
            market_value += value
            unrealized_pnl += pnl
            active_count += 1
            daily_position_rows.append(
                {
                    "date": trading_date,
                    "instrument_id": stock_to_instrument_id(stock_code, strategy),
                    "stock_code": stock_code,
                    "quantity": int(quantity),
                    "avg_cost": avg_cost,
                    "last_price": last_price,
                    "market_value": value,
                    "unrealized_pnl": pnl,
                },
            )

        total_equity = cash + market_value
        high_water = max(high_water, total_equity)
        daily_return = Decimal("0") if previous_total in (None, Decimal("0")) else total_equity / previous_total - 1
        net_value = total_equity / initial_cash
        cum_return = net_value - 1
        drawdown = Decimal("0") if high_water == 0 else total_equity / high_water - 1
        for row in daily_position_rows:
            row["weight"] = Decimal("0") if total_equity == 0 else row["market_value"] / total_equity
            position_rows.append(row)
        portfolio_rows.append(
            {
                "date": trading_date,
                "cash": cash,
                "market_value": market_value,
                "total_equity": total_equity,
                "net_value": net_value,
                "daily_return": daily_return,
                "cum_return": cum_return,
                "drawdown": drawdown,
                "unrealized_pnl": unrealized_pnl,
                "active_positions": active_count,
            },
        )
        previous_total = total_equity

    return pd.DataFrame(portfolio_rows), pd.DataFrame(position_rows)


def daily_cash_ledger(daily_portfolio: pd.DataFrame) -> pd.DataFrame:
    return REPORT_PROCESSOR.daily_cash_ledger(daily_portfolio)


def close_prices_by_date(
    args: argparse.Namespace,
    bars_by_stock: dict[str, list[Any]],
) -> tuple[dict[pd.Timestamp, dict[str, Decimal]], list[pd.Timestamp]]:
    close_by_date: dict[pd.Timestamp, dict[str, Decimal]] = {}
    for stock_code, bars in bars_by_stock.items():
        for bar in bars:
            trading_date = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(args.exchange_timezone).date()
            close_by_date.setdefault(pd.Timestamp(trading_date), {})[stock_code] = Decimal(str(float(bar.close)))
    return close_by_date, sorted(close_by_date)


def fills_by_exchange_date(
    args: argparse.Namespace,
    fills_report: Any,
    strategy: ModelPredictionsStrategy,
) -> dict[pd.Timestamp, list[dict[str, Any]]]:
    if not isinstance(fills_report, pd.DataFrame) or fills_report.empty:
        return {}
    result: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    stock_by_instrument = getattr(strategy, "_stock_by_instrument", {})
    for _, row in fills_report.reset_index().iterrows():
        instrument_id = str(first_value(row, "instrument_id", default=""))
        stock_code = stock_by_instrument.get(instrument_id, instrument_id.replace(".QMT", ""))
        event_time = timestamp_value(first_value(row, "ts_init", "ts_last", default=None), fallback_date=None)
        if event_time is None:
            continue
        trading_date = pd.Timestamp(event_time).tz_convert(args.exchange_timezone).date()
        side = side_text(first_value(row, "side", "order_side", default=""))
        quantity = Decimal(str(int_or_zero(first_value(row, "filled_qty", "quantity", "qty", default=0))))
        price = decimal_or_zero(first_value(row, "avg_px", "last_px", "price", default=0))
        if quantity <= 0 or price <= 0:
            continue
        result.setdefault(pd.Timestamp(trading_date), []).append(
            {
                "stock_code": stock_code,
                "side": side,
                "quantity": quantity,
                "price": price,
            },
        )
    return result


def stock_to_instrument_id(stock_code: str, strategy: ModelPredictionsStrategy) -> str:
    for instrument_id, mapped_stock_code in getattr(strategy, "_stock_by_instrument", {}).items():
        if mapped_stock_code == stock_code:
            return instrument_id
    return f"{stock_code}.QMT"


def strategy_signal_frame(strategy: ModelPredictionsStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_date": event.signal_date,
                "instrument_id": event.instrument_id,
                "stock_code": event.stock_code,
                "signal_name": event.signal_name,
                "score": event.score,
                "rank": event.rank,
                "side": event.side,
                "selected": event.selected,
                "extra": event.extra,
            }
            for event in strategy.signal_events
        ],
    )


def strategy_target_frame(strategy: ModelPredictionsStrategy) -> pd.DataFrame:
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


def strategy_order_frame(strategy: ModelPredictionsStrategy) -> pd.DataFrame:
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


def timestamp_value(value: Any, fallback_date: pd.Timestamp | None = None) -> datetime | None:
    return REPORT_PROCESSOR.timestamp_value(value, fallback_date=fallback_date)


def side_text(value: Any) -> str:
    return REPORT_PROCESSOR.side_text(value)


def main() -> None:
    args = parse_args()
    connection = build_connection(args)
    prediction_provider = ClickHouseModelPredictionDataProvider(connection)
    request = build_prediction_request(args)
    bundle = prediction_provider.load(request)
    print(
        "Loaded prediction data: "
        f"prediction_rows={bundle.prediction_rows} selected_rows={bundle.selected_rows} "
        f"universe={len(bundle.universe)} trading_dates={len(bundle.trading_dates)} "
        f"st_dates={sum(len(v) for v in bundle.st_by_date.values())} "
        f"suspensions={sum(len(v) for v in bundle.suspended_by_date.values())}."
    )
    if args.print_signals:
        print(bundle.to_frame().tail(100).to_string(index=False))
        return

    bar_types, bars_by_stock, skipped_rows = load_bars(args, connection, bundle)
    print(
        f"Loaded bars for {len(bars_by_stock)}/{len(bundle.universe)} instruments; "
        f"bars={sum(len(bars) for bars in bars_by_stock.values())} skipped_rows={skipped_rows}."
    )
    if not bars_by_stock:
        raise SystemExit("No valid bars loaded for the selected signal universe.")
    if args.load_only:
        return

    writer = build_result_writer(args)
    experiment_id_value = experiment_id()
    started_at = datetime.now()
    if writer is not None:
        writer.create_experiment(create_experiment_record(args, experiment_id_value, started_at))
        writer.write_experiment_params(experiment_params(args, experiment_id_value))

    engine, strategy = build_engine(args, bundle, bar_types, bars_by_stock)
    try:
        engine.run()
        complete_report = build_complete_report(args, engine, strategy, bars_by_stock)
        complete_report = apply_benchmark_to_reports(args, connection, complete_report)
        tearsheet_path = REPORT_PROCESSOR.write_tearsheet(args, engine, complete_report)
        if tearsheet_path:
            print(f"tearsheet written: {tearsheet_path}")
        if not args.skip_reports:
            print_reports(engine, complete_report)
        if args.report_dir:
            write_report_dir(args.report_dir, complete_report)
            print(f"complete report written: {args.report_dir}")
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
