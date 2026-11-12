#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import sys
import uuid
from datetime import date
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
from backtests.data_providers import ClickHouseDailyStockDataProvider  # noqa: E402
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
from backtests.result_writers.live_records import CONTINUOUS_TRADING  # noqa: E402
from backtests.result_writers.live_writer import LiveSnapshotWriter  # noqa: E402
from lives.snapshot_recorder import SnapshotRecorder  # noqa: E402
from lives.snapshot_recorder import SnapshotRecorderConfig  # noqa: E402
from market_data import DailyStockData  # noqa: E402
from strategies.convergence import ConvergenceTrigger  # noqa: E402
from strategies.model_prediction_targets import TargetModelPredictionsStrategy  # noqa: E402
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig  # noqa: E402
from strategies.strategy_params import StrategyParams  # noqa: E402
from nautilus_trader.core.data import Data  # noqa: E402
from nautilus_trader.model.data import Bar  # noqa: E402
from nautilus_trader.model.data import CustomData  # noqa: E402
from nautilus_trader.model.data import DataType  # noqa: E402
from nautilus_trader.model.data import QuoteTick  # noqa: E402
from nautilus_trader.model.identifiers import ClientId  # noqa: E402


STRATEGY_ID = os.getenv("BACKTEST_STRATEGY_ID", "nautilus_target_model_predictions")
STRATEGY_VERSION_ID = os.getenv("BACKTEST_STRATEGY_VERSION_ID", "dev")
REPORT_DECIMAL_QUANTUM = Decimal("0.001")
REPORT_PROCESSOR = BaseBacktest(REPORT_DECIMAL_QUANTUM, csv_index=True)
PLANNING_CLIENT_ID = ClientId("BACKTEST_PLANNING")
HEARTBEAT_TIME = "14:30:00"
PLANNING_TIME = "09:30:01"
EXECUTION_BAR_TIME = "14:30:01"


class DailyPlanningEvent(Data):
    """Backtest-only daily trigger carrying the live-equivalent full-tick snapshot."""

    def __init__(
        self,
        trading_date: date,
        full_tick_snapshot: dict[str, dict[str, Any]],
        previous_closes: dict[str, float],
        ts_event: int,
        ts_init: int,
        allow_planning: bool = True,
    ) -> None:
        self.allow_planning = allow_planning
        self.trading_date = trading_date
        self.full_tick_snapshot = full_tick_snapshot
        self.previous_closes = previous_closes
        self._ts_event = int(ts_event)
        self._ts_init = int(ts_init)

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init


class BacktestTargetModelPredictionsStrategy(TargetModelPredictionsStrategy):
    """Target-model strategy driven by explicit daily planning events, not bars."""

    def __init__(self, config: TargetModelPredictionsStrategyConfig) -> None:
        super().__init__(config)
        self._tick_missing_opens: dict[date, tuple[str, ...]] = {}

    def _price_limits(self, instrument_id_text: str) -> tuple[float | None, float | None]:
        if self._auction_replay_model is None:
            return super()._price_limits(instrument_id_text)
        # Historical daily limits belong to replay, never the shared live strategy.
        row = self._daily_stock_data.get((self._stock_code_for_instrument(instrument_id_text),
                                          self._clock_date()))
        return (None, None) if row is None else (row.up_limit, row.down_limit)

    def _process_trading_day_once(self, trading_date: date, trigger: str) -> bool:
        if trading_date in self._processed_dates:
            return False
        if self._auction_replay_model is not None:
            signal_date = self._resolve_signal_date(trading_date)
            required = self._held_instrument_ids() | {
                str(self._instrument_by_stock[signal["stock_code"]])
                for signal in self._signals_by_date.get(signal_date, [])
                if signal["stock_code"] in self._instrument_by_stock
                and self._entry_skip_reason(signal["stock_code"], trading_date) is None
            }
            suspended = self._suspended_by_date.get(trading_date, set())
            missing = sorted(i for i in required
                             if self._stock_code_for_instrument(i) not in suspended
                             and not self._is_suspended_status(i)
                             and self._today_open_price(i) is None)
            if missing:
                # Gate every entry point, including inherited timers and refreshes.
                # Missing data never freezes a partially populated comparison plan.
                if self._tick_missing_opens.get(trading_date) != tuple(missing):
                    self._tick_missing_opens[trading_date] = tuple(missing)
                    self.log.warning(f"Tick planning deferred: date={trading_date}, missing_opens={missing}")
                return False
            self._tick_missing_opens.pop(trading_date, None)
        return super()._process_trading_day_once(trading_date, trigger)

    def on_start(self) -> None:
        super().on_start()
        self.subscribe_data(
            DataType(DailyPlanningEvent),
            client_id=PLANNING_CLIENT_ID,
        )

    def _start_full_tick_refresh(self) -> None:
        # The historical full-tick equivalent arrives in DailyPlanningEvent.
        return

    def _refresh_order_book_depth_subscriptions(
        self,
        quantities: dict[str, Decimal],
    ) -> None:
        # Daily bars are the venue's execution data; no L2 stream exists in this backtest.
        return

    def on_data(self, data: Data) -> None:
        if not isinstance(data, DailyPlanningEvent):
            return
        self._roll_trading_day(data.trading_date)
        self.refresh_target_instruments(
            instrument_ids=[],
            bar_types={},
            last_closes=data.previous_closes,
            subscribe_new_bars=False,
        )
        self.apply_full_tick_snapshot(data.full_tick_snapshot, "backtest_planning")
        if self.config.auction_enabled:
            self.process_opening_auction()
        if data.allow_planning:
            self._process_trading_day_once(data.trading_date, "planning_event")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        super().on_quote_tick(tick)
        self._converge_to_target(
            current_date=bar_trading_date(tick, self.config.timezone_name),
            trigger=ConvergenceTrigger.ORDER_BOOK_DEPTH,
            ts_event=int(tick.ts_event),
        )


class BacktestTargetPlanRecorder(SnapshotRecorder):
    """Reuse live target persistence without installing live snapshot timers."""

    def on_start(self) -> None:
        return


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
        description="Run the target model-prediction strategy with Nautilus and QMT venue wiring.",
    )
    parser.add_argument("--start", default=env("BACKTEST_START_DATE", "2025-01-02"))
    parser.add_argument("--end", default=env("BACKTEST_END_DATE", "2025-12-31"))
    parser.add_argument(
        "--config",
        default=env("STRATEGY_CONFIG_FILE"),
        help="Path to the strategy-params YAML file (see configs/strategy.yaml). Required.",
    )
    parser.add_argument(
        "--predictions-table",
        default=env("TARGET_MODEL_PREDICTIONS_TABLE", "daily_model_predictions"),
    )
    parser.add_argument(
        "--predictions-database",
        default=env("TARGET_MODEL_PREDICTIONS_DATABASE"),
    )
    parser.add_argument("--stock-codes", default=",".join(env_list("MODEL_STOCK_CODES", "000001.SZ,000002.SZ")))
    parser.add_argument("--all-stocks", action="store_true", default=env_bool("MODEL_ALL_STOCKS", False))
    parser.add_argument("--excluded-stock-codes", default=",".join(env_list("MODEL_EXCLUDED_STOCK_CODES", "")))
    parser.add_argument("--min-score", type=float, default=parse_optional_float(env("MODEL_MIN_SCORE")))
    parser.add_argument("--top-frac", type=float, default=float(env("MODEL_TOP_FRAC", "0.10")))
    parser.add_argument("--max-positions", type=int, default=int(env("MODEL_MAX_POSITIONS", "50")))
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
    tick_source = parser.add_mutually_exclusive_group()
    tick_source.add_argument("--tick-table", default=env("BACKTEST_TICK_TABLE"),
                             help="RiceQuant ordinary tick table, e.g. ricequant_stock_tick.")
    tick_source.add_argument("--tick-file", default=env("BACKTEST_TICK_FILE"),
                        help="Historical auction/quote CSV; enables tick replay instead of synthetic daily fills.")
    parser.add_argument("--tick-data-latency-ms", type=float, default=0.0,
                        help="Simulated receive delay for RiceQuant exchange timestamps; ingest time is not receive time.")
    parser.add_argument("--compare-auction", action="store_true",
                        help="Run a second tick replay with auction execution disabled and report comparison.")
    parser.add_argument("--auction-participation-rate", type=float, default=0.01)
    parser.add_argument("--submission-latency-ms", type=float, default=0.0)
    parser.add_argument("--cancel-latency-ms", type=float, default=100.0)
    parser.add_argument("--commission-rate", type=float, default=None,
                        help="Brokerage rate; defaults to auction_commission_rate from strategy YAML.")
    parser.add_argument("--minimum-commission", type=float, default=None,
                        help="Minimum brokerage per order; defaults to auction_minimum_fee from strategy YAML.")
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--strict-data", action="store_true")
    parser.add_argument("--print-signals", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--report-dir", default=env("BACKTEST_REPORT_PATH"))
    parser.add_argument("--write-results", action="store_true", default=env_bool("BACKTEST_RESULT_WRITE_ENABLED", False))
    add_benchmark_args(parser)
    REPORT_PROCESSOR.add_tearsheet_args(parser)
    args = parser.parse_args()
    if not args.config:
        parser.error("--config is required, or set STRATEGY_CONFIG_FILE (see configs/strategy.yaml)")
    params = StrategyParams.from_yaml(args.config)
    if args.commission_rate is None:
        args.commission_rate = params.auction_commission_rate
    if args.minimum_commission is None:
        args.minimum_commission = params.auction_minimum_fee
    if args.tick_file and args.tick_table:
        parser.error("select only one tick source: --tick-file or --tick-table")
    if params.auction_enabled and not (args.tick_file or args.tick_table):
        parser.error("auction_enabled requires --tick-file or --tick-table; daily bars cannot simulate opening auction")
    if not 0 < args.auction_participation_rate <= 1:
        parser.error("--auction-participation-rate must be in (0, 1]")
    if any(not math.isfinite(value) or value < 0 for value in (
        args.submission_latency_ms, args.cancel_latency_ms, args.tick_data_latency_ms,
        args.commission_rate, args.minimum_commission, args.stamp_duty_rate,
    )):
        parser.error("replay latency and fees must be finite and non-negative")
    if args.compare_auction and not (args.tick_file or args.tick_table):
        parser.error("--compare-auction requires --tick-file or --tick-table")
    if args.compare_auction and not params.auction_enabled:
        parser.error("--compare-auction requires auction_enabled: true in strategy config")
    return args


def build_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
    return REPORT_PROCESSOR.build_clickhouse_connection(args)


def build_prediction_request(
    args: argparse.Namespace,
    trading_history_days: int = 0,
) -> ModelPredictionDataRequest:
    return ModelPredictionDataRequest(
        start_date=args.start,
        end_date=args.end,
        predictions_database=args.predictions_database,
        predictions_table=args.predictions_table,
        stock_codes=env_list_from_value(args.stock_codes),
        all_stocks=args.all_stocks,
        excluded_stock_codes=set(env_list_from_value(args.excluded_stock_codes)),
        min_score=args.min_score,
        top_frac=args.top_frac,
        max_positions=args.max_positions,
        signal_warmup_days=args.signal_warmup_days,
        trading_history_days=trading_history_days,
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


def exchange_timestamp_ns(
    trading_date: date,
    time_text: str,
    timezone_name: str,
) -> int:
    timestamp = pd.Timestamp(f"{trading_date.isoformat()} {time_text}")
    return int(timestamp.tz_localize(timezone_name).tz_convert("UTC").value)


def bar_trading_date(bar: Bar, timezone_name: str) -> date:
    return pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(timezone_name).date()


def bar_with_timestamp(bar: Bar, timestamp_ns: int) -> Bar:
    return Bar(
        bar_type=bar.bar_type,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        ts_event=timestamp_ns,
        ts_init=timestamp_ns,
    )


def prepare_backtest_event_data(
    args: argparse.Namespace,
    bundle: PredictionDataBundle,
    bars_by_stock: dict[str, list[Bar]],
    instruments_by_stock: dict[str, Any],
) -> tuple[list[CustomData], list[QuoteTick], dict[str, list[Bar]]]:
    """
    Build a deterministic live-like daily event sequence.

    The 09:30 planning snapshot is the strategy input. Per-instrument synthetic
    quotes open the 14:30 execution gate, and bars arrive one second afterward
    solely for venue matching and portfolio valuation. Daily data has no historical
    L2, so each synthetic same-side best uses that day's open.
    """
    raw_bars_by_date: dict[date, dict[str, Bar]] = {}
    for stock_code, bars in bars_by_stock.items():
        for bar in bars:
            trading_date = bar_trading_date(bar, args.exchange_timezone)
            if stock_code in raw_bars_by_date.setdefault(trading_date, {}):
                raise RuntimeError(
                    f"multiple daily bars for {stock_code} on {trading_date}",
                )
            raw_bars_by_date[trading_date][stock_code] = bar

    configured_dates = {
        value
        for value in bundle.trading_dates
        if pd.Timestamp(args.start).date() <= value <= pd.Timestamp(args.end).date()
    }
    trading_dates = sorted(configured_dates.union(raw_bars_by_date))
    previous_closes: dict[str, float] = {}
    normalized_bars: dict[str, list[Bar]] = {stock_code: [] for stock_code in bars_by_stock}
    planning_events: list[CustomData] = []
    heartbeats: list[QuoteTick] = []
    data_type = DataType(DailyPlanningEvent)

    for trading_date in trading_dates:
        bars_for_date = raw_bars_by_date.get(trading_date, {})
        suspended = bundle.suspended_by_date.get(trading_date, set())
        snapshot: dict[str, dict[str, Any]] = {}

        for stock_code in sorted(bars_by_stock):
            instrument = instruments_by_stock[stock_code]
            instrument_id = str(instrument.id)
            bar = bars_for_date.get(stock_code)
            last_close = previous_closes.get(stock_code)
            if stock_code in suspended:
                snapshot[instrument_id] = {
                    "open": 0.0,
                    "last_price": last_close,
                    "last_close": last_close,
                    "open_int": 1,
                }
                continue
            if bar is None:
                continue
            open_price = float(bar.open)
            snapshot[instrument_id] = {
                "open": open_price,
                "last_price": open_price,
                "last_close": last_close,
                "open_int": 13,
            }

        planning_ts = exchange_timestamp_ns(
            trading_date,
            PLANNING_TIME,
            args.exchange_timezone,
        )
        planning_event = DailyPlanningEvent(
            trading_date=trading_date,
            full_tick_snapshot=snapshot,
            previous_closes={
                str(instruments_by_stock[stock_code].id): close
                for stock_code, close in previous_closes.items()
            },
            ts_event=planning_ts,
            ts_init=planning_ts,
        )
        planning_events.append(CustomData(data_type, planning_event))

        for heartbeat_stock, heartbeat_bar in sorted(bars_for_date.items()):
            if heartbeat_stock in suspended:
                continue
            heartbeat_instrument = instruments_by_stock[heartbeat_stock]
            heartbeat_ts = exchange_timestamp_ns(
                trading_date,
                HEARTBEAT_TIME,
                args.exchange_timezone,
            )
            open_price = float(heartbeat_bar.open)
            heartbeats.append(
                QuoteTick(
                    instrument_id=heartbeat_instrument.id,
                    bid_price=heartbeat_instrument.make_price(open_price),
                    ask_price=heartbeat_instrument.make_price(open_price),
                    bid_size=heartbeat_instrument.make_qty(100),
                    ask_size=heartbeat_instrument.make_qty(100),
                    ts_event=heartbeat_ts,
                    ts_init=heartbeat_ts,
                ),
            )

        execution_ts = exchange_timestamp_ns(
            trading_date,
            EXECUTION_BAR_TIME,
            args.exchange_timezone,
        )
        for stock_code, bar in sorted(bars_for_date.items()):
            normalized_bars[stock_code].append(bar_with_timestamp(bar, execution_ts))
            previous_closes[stock_code] = float(bar.close)

    return planning_events, heartbeats, normalized_bars


def prepare_tick_replay_event_data(args, rows, instruments_by_stock):
    from backtests.data_providers.historical_ticks import validate_final_auction_results

    validate_final_auction_results(rows, args.exchange_timezone)
    events = []
    quotes = []
    batches = {}
    for row in rows:
        instrument = instruments_by_stock[row.symbol]
        trading_date = pd.Timestamp(row.ts_event, unit="ns", tz="UTC").tz_convert(
            args.exchange_timezone).date()
        quotes.append(QuoteTick(
            instrument.id, instrument.make_price(row.bid_price), instrument.make_price(row.ask_price),
            instrument.make_qty(row.bid_size), instrument.make_qty(row.ask_size),
            row.ts_event, row.ts_init,
        ))
        # Apply all observations received together before daily planning. A
        # per-symbol event would freeze a plan with only the first actual open.
        key = (row.ts_init, trading_date)
        batch = batches.setdefault(key, dict(snapshot={}, previous_closes={},
                                             ts_event=0, allow_planning=False))
        batch["snapshot"][str(instrument.id)] = {
            "open": row.open_price, "last_price": row.bid_price,
            "last_close": row.previous_close,
            "open_int": 12 if row.kind == "indicative" else 13,
        }
        batch["previous_closes"][str(instrument.id)] = row.previous_close
        batch["ts_event"] = max(batch["ts_event"], row.ts_event)
        batch["allow_planning"] |= row.kind != "indicative"
    for (ts_init, trading_date), batch in sorted(batches.items()):
        event = DailyPlanningEvent(
            trading_date, batch["snapshot"], batch["previous_closes"],
            batch["ts_event"], ts_init, allow_planning=batch["allow_planning"],
        )
        events.append(CustomData(DataType(DailyPlanningEvent), event))
    return events, quotes


def prepare_tick_replay_depth_data(rows, instruments_by_stock):
    """Construct a one-level simulated book from ordinary ticks, without L2 data."""
    from nautilus_trader.model.data import BookOrder, OrderBookDepth10
    from nautilus_trader.model.enums import OrderSide

    depths = []
    for sequence, row in enumerate(rows, 1):
        instrument = instruments_by_stock[row.symbol]
        depths.append(OrderBookDepth10(
            instrument.id,
            [BookOrder(OrderSide.BUY, instrument.make_price(row.bid_price),
                       instrument.make_qty(row.bid_size), 1)],
            [BookOrder(OrderSide.SELL, instrument.make_price(row.ask_price),
                       instrument.make_qty(row.ask_size), 2)],
            [1], [1], 0, sequence, row.ts_event, row.ts_init,
        ))
    return depths


def load_tick_replay_data(args, bundle, bars_by_stock):
    if args.exchange_timezone != "Asia/Shanghai":
        raise ValueError("A-share tick replay requires Asia/Shanghai exchange timezone")
    symbols = {
        signal.stock_code for signals in bundle.signals_by_date.values() for signal in signals
        if signal.stock_code in bars_by_stock
    }
    if args.tick_table:
        from backtests.data_providers.ricequant_ticks import ClickHouseRicequantTickDataProvider
        return ClickHouseRicequantTickDataProvider(
            build_connection(args), args.tick_table, args.tick_data_latency_ms,
        ).load(start=args.start, end=args.end, symbols=symbols)
    from backtests.data_providers.historical_ticks import HistoricalTickDataProvider
    return HistoricalTickDataProvider().load(
        args.tick_file, start=args.start, end=args.end, symbols=symbols,
        timezone=args.exchange_timezone,
    )


def build_engine(
    args: argparse.Namespace,
    bundle: PredictionDataBundle,
    bar_types: dict[str, Any],
    bars_by_stock: dict[str, list[Any]],
    daily_stock_data: tuple[DailyStockData, ...],
    *, auction_enabled: bool | None = None,
) -> tuple[Any, BacktestTargetModelPredictionsStrategy, dict[str, list[Bar]]]:
    from nautilus_trader.adapters.qmt.common import parse_equity
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.adapters.qmt.constants import QMT_VENUE
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import RiskEngineConfig
    from nautilus_trader.model.currencies import CNY
    from nautilus_trader.model.enums import AccountType
    from nautilus_trader.model.enums import BookType
    from nautilus_trader.model.enums import OmsType
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.model.objects import Money

    params = StrategyParams.from_yaml(args.config)
    from dataclasses import replace

    if args.tick_file or args.tick_table:
        # Reserve the same brokerage charged by replay when CLI overrides YAML.
        params = replace(params, auction_commission_rate=args.commission_rate,
                         auction_minimum_fee=args.minimum_commission)
    if auction_enabled is not None:
        params = replace(params, auction_enabled=auction_enabled)

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(args.trader_id),
            logging=LoggingConfig(log_level=args.log_level),
            risk_engine=RiskEngineConfig(bypass=True),
        ),
    )
    params.log(engine.logger, source=args.config)
    historical_ticks = None
    replay_model = None
    replay_options = {}
    if args.tick_file or args.tick_table:
        from backtests.target_model_predictions.auction_simulation import AuctionReplayFillModel
        from backtests.target_model_predictions.auction_simulation import AuctionReplayModule
        from backtests.target_model_predictions.auction_simulation import AShareReplayFeeModel
        from nautilus_trader.backtest.models import LatencyModel

        historical_ticks = load_tick_replay_data(args, bundle, bars_by_stock)
        replay_model = AuctionReplayFillModel(engine.kernel.clock.timestamp_ns,
                                             args.auction_participation_rate)
        rows = {(str(qmt_symbol_to_instrument_id(row.symbol)), row.ts_init): row
                for row in historical_ticks}
        if len(rows) != len(historical_ticks):
            raise ValueError("Historical replay contains ambiguous symbol/receive timestamps")
        replay_options = dict(
            fill_model=replay_model,
            modules=[AuctionReplayModule(replay_model, rows)],
            fee_model=AShareReplayFeeModel(args.commission_rate, args.minimum_commission,
                                          args.stamp_duty_rate),
            latency_model=LatencyModel(base_latency_nanos=0, insert_latency_nanos=int(args.submission_latency_ms * 1e6),
                                       cancel_latency_nanos=int(args.cancel_latency_ms * 1e6)),
            trade_execution=False, liquidity_consumption=True,
            book_type=BookType.L2_MBP,
        )
    elif params.auction_enabled:
        raise ValueError("auction_enabled requires tick replay data")
    engine.add_venue(
        venue=QMT_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=CNY,
        starting_balances=[Money(args.starting_cash, CNY)],
        default_leverage=Decimal(1),
        bar_execution=historical_ticks is None,
        **replay_options,
    )

    loaded_stock_codes = sorted(bars_by_stock)
    instrument_ids = []
    config_bar_types = {}
    instrument_stock_codes = {}
    instruments_by_stock = {}
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
        if instrument.id != instrument_id:
            raise RuntimeError(
                f"parsed instrument id mismatch for {stock_code}: "
                f"{instrument.id} != {instrument_id}",
            )
        instruments_by_stock[stock_code] = instrument
        instrument_ids.append(instrument_id)
        config_bar_types[str(instrument_id)] = bar_types[stock_code]
        instrument_stock_codes[str(instrument_id)] = stock_code

    if historical_ticks is None:
        planning_events, heartbeats, execution_bars_by_stock = prepare_backtest_event_data(
            args=args,
            bundle=bundle,
            bars_by_stock=bars_by_stock,
            instruments_by_stock=instruments_by_stock,
        )
    else:
        planning_events, heartbeats = prepare_tick_replay_event_data(
            args, historical_ticks, instruments_by_stock,
        )
        # Daily bars are retained solely for independent closing valuation.
        execution_bars_by_stock = bars_by_stock
    execution_bars = [
        bar
        for stock_code in loaded_stock_codes
        for bar in execution_bars_by_stock[stock_code]
    ]
    if historical_ticks is not None:
        engine.add_data(prepare_tick_replay_depth_data(historical_ticks, instruments_by_stock), sort=False)
    if heartbeats:
        engine.add_data(heartbeats, sort=False)
    if planning_events:
        engine.add_data(planning_events, client_id=PLANNING_CLIENT_ID, sort=False)
    if historical_ticks is None:
        engine.add_data(execution_bars, sort=False)
    engine.sort_data()

    strategy = BacktestTargetModelPredictionsStrategy(
        config=TargetModelPredictionsStrategyConfig(
            instrument_ids=instrument_ids,
            bar_types=config_bar_types,
            instrument_stock_codes=instrument_stock_codes,
            signals_by_date=signals_config(bundle, set(loaded_stock_codes)),
            prediction_ranks_by_date={
                key.isoformat(): values
                for key, values in bundle.prediction_ranks_by_date.items()
            },
            trading_dates=[value.isoformat() for value in bundle.trading_dates],
            listed_dates={key: value.isoformat() for key, value in bundle.listed_dates.items()},
            st_by_date={key.isoformat(): sorted(values) for key, values in bundle.st_by_date.items()},
            suspended_by_date={
                key.isoformat(): sorted(values)
                for key, values in bundle.suspended_by_date.items()
            },
            daily_stock_data=daily_stock_data,
            consecutive_up_limit_days=params.consecutive_up_limit_days,
            max_open_gap_up=params.max_open_gap_up,
            max_positions=args.max_positions,
            max_position_percent=params.max_position_percent,
            holding_days=params.holding_days,
            stop_loss=params.stop_loss,
            trailing_take_profit=params.trailing_take_profit,
            trailing_take_profit_start=params.trailing_take_profit_start,
            min_listed_days=params.min_listed_days,
            initial_cash=args.starting_cash,
            timezone_name=args.exchange_timezone,
            excluded_name_prefixes=params.excluded_name_prefixes,
            target_cash_buffer_percent=params.target_cash_buffer_percent,
            cash_buffer_percent=params.cash_buffer_percent,
            target_weight_planner=params.target_weight_planner,
            target_weight_planner_error_policy=params.target_weight_planner_error_policy,
            local_exit_authoritative=params.local_exit_authoritative,
            risk_manager_base_url=params.risk_manager_base_url,
            risk_manager_risk_model_id=params.risk_manager_risk_model_id,
            alpha_model_id=params.alpha_model_id,
            risk_manager_mode=params.risk_manager_mode,
            risk_manager_timeout_secs=params.risk_manager_timeout_secs,
            order_slice_notional=params.order_slice_notional,
            limit_stop_mode=params.limit_stop_mode,
            exit_non_targets=params.exit_non_targets,
            trading_windows=params.trading_windows,
            exchange_trading_windows=params.exchange_trading_windows,
            auction_enabled=params.auction_enabled,
            auction_prepare_time=params.auction_prepare_time,
            auction_submit_time=params.auction_submit_time,
            auction_submit_cutoff=params.auction_submit_cutoff,
            auction_reference_max_age_secs=params.auction_reference_max_age_secs,
            auction_buy_offset_bps=params.auction_buy_offset_bps,
            auction_sell_offset_bps=params.auction_sell_offset_bps,
            auction_commission_rate=params.auction_commission_rate,
            auction_minimum_fee=params.auction_minimum_fee,
            auction_partial_min_notional=params.auction_partial_min_notional,
            execution_windows=params.execution_windows,
            exchange_execution_windows=params.exchange_execution_windows,
            cancel_windows=params.cancel_windows,
            exchange_cancel_windows=params.exchange_cancel_windows,
            stop_time=params.stop_time,
            require_account_cash=True,
            submission_book_max_age_secs=params.submission_book_max_age_secs,
            cancel_book_max_age_secs=params.cancel_book_max_age_secs,
            same_side_price_improvement_threshold=(
                params.same_side_price_improvement_threshold
            ),
            price_mismatch_cancel_delay_secs=params.price_mismatch_cancel_delay_secs,
            partial_fill_cancel_grace_secs=params.partial_fill_cancel_grace_secs,
            partial_fill_notional_threshold=params.partial_fill_notional_threshold,
            small_leaves_notional_threshold=params.small_leaves_notional_threshold,
            price_trend_effect_enabled=params.price_trend_effect_enabled,
            price_trend_window_secs=params.price_trend_window_secs,
            price_trend_threshold_bps=params.price_trend_threshold_bps,
            price_trend_max_bps=params.price_trend_max_bps,
            price_trend_confirmation_strength=params.price_trend_confirmation_strength,
            price_trend_book_max_age_secs=params.price_trend_book_max_age_secs,
            price_trend_min_timeout_multiplier=params.price_trend_min_timeout_multiplier,
            price_trend_max_offset_multiplier=params.price_trend_max_offset_multiplier,
            subscribe_bars=False,
            subscribe_quote_ticks=True,
            subscribe_trade_ticks=False,
            subscribe_order_book_depth=False,
        ),
    )
    strategy._auction_replay_model = replay_model
    engine.add_strategy(strategy)
    return engine, strategy, execution_bars_by_stock


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
    return MySQLResultWriter.from_pymysql_kwargs(**mysql_connection_kwargs())


def mysql_connection_kwargs() -> dict[str, Any]:
    return {
        "host": env("MYSQL_HOST", "localhost"),
        "port": int(env("MYSQL_PORT", "3306")),
        "user": env("MYSQL_USER", "root"),
        "password": env("MYSQL_PASSWORD", ""),
        "database": env("MYSQL_DATABASE", "backtest"),
        "charset": "utf8mb4",
    }


def configure_target_plan_persistence(
    args: argparse.Namespace,
    engine: Any,
    strategy: BacktestTargetModelPredictionsStrategy,
    experiment_id_value: str,
) -> LiveSnapshotWriter | None:
    if not args.write_results:
        return None
    writer = LiveSnapshotWriter.from_pymysql_kwargs(
        logger=None,
        target_portfolio_table=env(
            "BACKTEST_TARGET_PORTFOLIO_TABLE",
            "backtest_target_portfolio",
        ),
        **mysql_connection_kwargs(),
    )

    def load_target_portfolio(trading_date: date, signal_date: date | None):
        return writer.load_target_portfolios(
            experiment_id_value,
            str(args.trader_id),
            trading_date,
            signal_date,
            preferred_snapshot_type=CONTINUOUS_TRADING,
        )

    def load_recent_target_dates(
        trading_date: date,
        cutoff_trade_date: date,
        stock_codes: list[str],
    ):
        return writer.load_recent_target_dates(
            experiment_id_value,
            str(args.trader_id),
            trading_date,
            cutoff_trade_date,
            stock_codes,
        )

    strategy.configure_live_target_portfolio_loader(load_target_portfolio)
    strategy.configure_recent_target_loader(load_recent_target_dates)
    recorder = BacktestTargetPlanRecorder(
        config=SnapshotRecorderConfig(
            account_id=experiment_id_value,
            trader_id=str(args.trader_id),
            timezone_name=args.exchange_timezone,
            before_time="",
            after_time="",
        ),
        writer=writer,
        strategy_ref=strategy,
    )
    strategy.configure_live_target_plan_persister(recorder.persist_strategy_target_plan)
    engine.add_actor(recorder)
    return writer


def experiment_id() -> str:
    return env("BACKTEST_EXPERIMENT_ID") or f"nautilus-target-model-predictions-{uuid.uuid4().hex[:12]}"


def create_experiment_record(args: argparse.Namespace, experiment_id_value: str, started_at: datetime) -> ExperimentRecord:
    benchmark_config = benchmark_config_from_args(args)
    return ExperimentRecord(
        experiment_id=experiment_id_value,
        experiment_name=env("BACKTEST_EXPERIMENT_NAME", "Nautilus Target Model Predictions"),
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
    params = StrategyParams.from_yaml(args.config)
    return {
        "strategy.predictions_table": args.predictions_table,
        "strategy.stock_codes": args.stock_codes,
        "strategy.all_stocks": args.all_stocks,
        "strategy.excluded_stock_codes": args.excluded_stock_codes,
        "strategy.min_score": args.min_score,
        "strategy.top_frac": args.top_frac,
        "strategy.max_positions": args.max_positions,
        "strategy.max_position_percent": params.max_position_percent,
        "strategy.consecutive_up_limit_days": params.consecutive_up_limit_days,
        "strategy.max_open_gap_up": params.max_open_gap_up,
        "strategy.holding_days": params.holding_days,
        "strategy.stop_loss": params.stop_loss,
        "strategy.trailing_take_profit": params.trailing_take_profit,
        "strategy.trailing_take_profit_start": params.trailing_take_profit_start,
        "strategy.min_listed_days": params.min_listed_days,
        "strategy.target_cash_buffer_percent": params.target_cash_buffer_percent,
        "strategy.target_weight_planner": params.target_weight_planner,
        "strategy.target_weight_planner_error_policy": params.target_weight_planner_error_policy,
        "strategy.local_exit_authoritative": params.local_exit_authoritative,
        "strategy.risk_manager_base_url": params.risk_manager_base_url,
        "strategy.risk_manager_risk_model_id": params.risk_manager_risk_model_id,
        "strategy.alpha_model_id": params.alpha_model_id,
        "strategy.risk_manager_mode": params.risk_manager_mode,
        "strategy.risk_manager_timeout_secs": params.risk_manager_timeout_secs,
        "strategy.order_slice_notional": params.order_slice_notional,
        "strategy.execution_windows": params.execution_windows,
        "strategy.exchange_execution_windows": params.exchange_execution_windows,
        "strategy.cancel_windows": params.cancel_windows,
        "strategy.exchange_cancel_windows": params.exchange_cancel_windows,
        "strategy.auction_commission_rate": params.auction_commission_rate,
        "strategy.auction_minimum_fee": params.auction_minimum_fee,
        "strategy.auction_partial_min_notional": params.auction_partial_min_notional,
        "strategy.submission_book_max_age_secs": params.submission_book_max_age_secs,
        "strategy.cancel_book_max_age_secs": params.cancel_book_max_age_secs,
        "strategy.same_side_price_improvement_threshold": (
            params.same_side_price_improvement_threshold
        ),
        "strategy.price_mismatch_cancel_delay_secs": params.price_mismatch_cancel_delay_secs,
        "strategy.partial_fill_cancel_grace_secs": params.partial_fill_cancel_grace_secs,
        "strategy.partial_fill_notional_threshold": params.partial_fill_notional_threshold,
        "strategy.small_leaves_notional_threshold": params.small_leaves_notional_threshold,
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
    strategy: TargetModelPredictionsStrategy,
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


def signal_records(experiment_id_value: str, strategy: TargetModelPredictionsStrategy) -> list[SignalRecord]:
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


def target_records(experiment_id_value: str, strategy: TargetModelPredictionsStrategy) -> list[TargetPortfolioRecord]:
    return [
        TargetPortfolioRecord(
            experiment_id=experiment_id_value,
            target_id=event.target_id,
            target_date=event.target_date,
            execute_date=event.execute_date,
            instrument_id=event.instrument_id,
            target_qty=int(event.target_qty),
            current_qty=None if event.current_qty is None else int(event.current_qty),
            delta_qty=None if event.delta_qty is None else int(event.delta_qty),
            source_signal_name="model_prediction_score",
            reason=event.reason,
            extra=event.extra,
        )
        for event in strategy.target_events
    ]


def order_records(experiment_id_value: str, strategy: TargetModelPredictionsStrategy) -> list[OrderRecord]:
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
                order_type="target_quantity",
                price_type="market",
                quantity=event.quantity,
                target_qty=int(event.target_qty),
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
    strategy: TargetModelPredictionsStrategy,
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
    strategy: TargetModelPredictionsStrategy,
    bars_by_stock: dict[str, list[Any]],
) -> dict[str, pd.DataFrame]:
    raw_reports = REPORT_PROCESSOR.raw_engine_reports(engine)
    if strategy._auction_replay_model is not None:
        raw_reports["fills"] = engine.trader.generate_fills_report()
    fills_report = raw_reports["fills"]
    daily_portfolio, daily_positions = reconstruct_daily_portfolio(
        args=args,
        strategy=strategy,
        fills_report=fills_report,
        bars_by_stock=bars_by_stock,
    )
    replay_reports = {}
    if strategy._auction_replay_model is not None:
        from backtests.target_model_predictions.auction_simulation import auction_execution_report
        replay_reports["auction_execution"] = auction_execution_report(
            engine, strategy, strategy._auction_replay_model)
    return REPORT_PROCESSOR.complete_report(
        engine=engine,
        daily_portfolio=daily_portfolio,
        daily_positions=daily_positions,
        raw_reports=raw_reports,
        extra_reports={
            **replay_reports,
            "signals": strategy_signal_frame(strategy),
            "targets": strategy_target_frame(strategy),
            "strategy_orders": strategy_order_frame(strategy),
        },
    )


def reconstruct_daily_portfolio(
    args: argparse.Namespace,
    strategy: TargetModelPredictionsStrategy,
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
            cash -= fill["commission"]
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
    strategy: TargetModelPredictionsStrategy,
) -> dict[pd.Timestamp, list[dict[str, Any]]]:
    if not isinstance(fills_report, pd.DataFrame) or fills_report.empty:
        return {}
    result: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    stock_by_instrument = strategy._stock_by_instrument
    for _, row in fills_report.reset_index().iterrows():
        instrument_id = str(first_value(row, "instrument_id", default=""))
        stock_code = stock_by_instrument.get(instrument_id, instrument_id.replace(".QMT", ""))
        event_time = timestamp_value(first_value(row, "ts_init", "ts_last", default=None), fallback_date=None)
        if event_time is None:
            continue
        trading_date = pd.Timestamp(event_time).tz_convert(args.exchange_timezone).date()
        side = side_text(first_value(row, "side", "order_side", default=""))
        quantity = Decimal(str(int_or_zero(first_value(row, "last_qty", "filled_qty", "quantity", "qty", default=0))))
        price = decimal_or_zero(first_value(row, "avg_px", "last_px", "price", default=0))
        if quantity <= 0 or price <= 0:
            continue
        result.setdefault(pd.Timestamp(trading_date), []).append(
            {
                "stock_code": stock_code,
                "side": side,
                "quantity": quantity,
                "price": price,
                "commission": Decimal(str(first_value(row, "commission", default=0)).split()[0].replace(",", "")),
            },
        )
    return result


def stock_to_instrument_id(stock_code: str, strategy: TargetModelPredictionsStrategy) -> str:
    for instrument_id, mapped_stock_code in strategy._stock_by_instrument.items():
        if mapped_stock_code == stock_code:
            return instrument_id
    return f"{stock_code}.QMT"


def strategy_signal_frame(strategy: TargetModelPredictionsStrategy) -> pd.DataFrame:
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


def strategy_target_frame(strategy: TargetModelPredictionsStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "target_id": event.target_id,
                "target_date": event.target_date,
                "execute_date": event.execute_date,
                "instrument_id": event.instrument_id,
                "target_qty": event.target_qty,
                "current_qty": event.current_qty,
                "delta_qty": event.delta_qty,
                "reason": event.reason,
                "extra": event.extra,
            }
            for event in strategy.target_events
        ],
    )


def strategy_order_frame(strategy: TargetModelPredictionsStrategy) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "order_id": event.order_id,
                "trading_date": event.trading_date,
                "instrument_id": event.instrument_id,
                "side": event.side,
                "quantity": event.quantity,
                "target_qty": event.target_qty,
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
    params = StrategyParams.from_yaml(args.config)
    connection = build_connection(args)
    prediction_provider = ClickHouseModelPredictionDataProvider(connection)
    history_days = max(int(params.consecutive_up_limit_days) - 1, 0)
    request = build_prediction_request(args, trading_history_days=history_days)
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
    daily_stock_data = ClickHouseDailyStockDataProvider(connection).load(
        stock_codes=sorted(bars_by_stock),
        start_date=min(bundle.trading_dates),
        end_date=args.end,
    )
    if args.load_only:
        if args.tick_file or args.tick_table:
            ticks = load_tick_replay_data(args, bundle, bars_by_stock)
            print(f"Loaded {len(ticks)} usable historical ticks for "
                  f"{len({tick.symbol for tick in ticks})} instruments; "
                  f"final_auction_results={sum(tick.kind == 'auction' for tick in ticks)}.")
        return

    writer = build_result_writer(args)
    experiment_id_value = experiment_id()
    started_at = datetime.now()
    if writer is not None:
        writer.create_experiment(create_experiment_record(args, experiment_id_value, started_at))
        writer.write_experiment_params(experiment_params(args, experiment_id_value))

    engine, strategy, execution_bars_by_stock = build_engine(
        args,
        bundle,
        bar_types,
        bars_by_stock,
        daily_stock_data,
    )
    target_plan_writer = configure_target_plan_persistence(
        args,
        engine,
        strategy,
        experiment_id_value,
    )
    try:
        engine.run()
        complete_report = build_complete_report(
            args,
            engine,
            strategy,
            execution_bars_by_stock,
        )
        if args.compare_auction:
            baseline_engine, baseline_strategy, baseline_bars = build_engine(
                args, bundle, bar_types, bars_by_stock, daily_stock_data, auction_enabled=False,
            )
            try:
                baseline_engine.run()
                baseline_report = build_complete_report(args, baseline_engine, baseline_strategy, baseline_bars)
                combined = []
                for label, report in [("auction", complete_report), ("continuous_only", baseline_report)]:
                    portfolio = report["daily_portfolio"]
                    row = portfolio.iloc[-1]
                    outcomes = report["auction_execution"]
                    combined.append(dict(
                        scenario=label, final_equity=row["total_equity"], final_cash=row["cash"],
                        final_net_value=row["net_value"],
                        auction_filled_amount=0 if outcomes.empty else outcomes.auction_filled_amount.sum(),
                        auction_submitted_quantity=0 if outcomes.empty else outcomes.submitted_quantity.sum(),
                        auction_filled_quantity=0 if outcomes.empty else outcomes.auction_filled_quantity.sum(),
                        participation_rate=args.auction_participation_rate,
                        submission_latency_ms=args.submission_latency_ms,
                        cancel_latency_ms=args.cancel_latency_ms,
                        tick_data_latency_ms=args.tick_data_latency_ms,
                    ))
                complete_report["auction_comparison"] = pd.DataFrame(combined)
                complete_report["continuous_only_daily_portfolio"] = baseline_report["daily_portfolio"]
            finally:
                baseline_engine.dispose()
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
        if target_plan_writer is not None:
            target_plan_writer.close()
        engine.dispose()


if __name__ == "__main__":
    main()
