#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from backtests.model_predictions import run_backtest as legacy  # noqa: E402
from strategies.model_prediction_targets import TargetModelPredictionsStrategy  # noqa: E402
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig  # noqa: E402


def env_decimal(name: str, default: str) -> Decimal:
    return Decimal((os.environ.get(name) or default).replace(",", ""))


def build_engine(
    args: Any,
    bundle: Any,
    bar_types: dict[str, Any],
    bars_by_stock: dict[str, list[Any]],
) -> tuple[Any, TargetModelPredictionsStrategy]:
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
            symbol=legacy.qmt_symbol(stock_code),
            fields={
                "name": bundle.instrument_names.get(stock_code, stock_code),
                "source": "clickhouse",
            },
            ts_event=bars[0].ts_init,
            ts_init=bars[0].ts_init,
        )
        engine.add_instrument(instrument)
        instrument_id = qmt_symbol_to_instrument_id(legacy.qmt_symbol(stock_code))
        instrument_ids.append(instrument_id)
        config_bar_types[str(instrument_id)] = bar_types[stock_code]
        instrument_stock_codes[str(instrument_id)] = stock_code
        all_bars.extend(bars)

    engine.add_data(sorted(all_bars, key=lambda bar: bar.ts_init))
    strategy = TargetModelPredictionsStrategy(
        config=TargetModelPredictionsStrategyConfig(
            instrument_ids=instrument_ids,
            bar_types=config_bar_types,
            instrument_stock_codes=instrument_stock_codes,
            signals_by_date=legacy.signals_config(bundle, set(loaded_stock_codes)),
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
            target_cash_buffer_percent=float(os.environ.get("MODEL_TARGET_CASH_BUFFER_PERCENT", "0.05")),
            weight_tolerance_percent=float(os.environ.get("MODEL_WEIGHT_TOLERANCE_PERCENT", "0.003")),
            cash_tolerance_percent=float(os.environ.get("MODEL_CASH_TOLERANCE_PERCENT", "0.01")),
            order_slice_notional=env_decimal("MODEL_ORDER_SLICE_NOTIONAL", "300000"),
            require_account_cash=False,
        ),
    )
    engine.add_strategy(strategy)
    return engine, strategy


def main() -> None:
    legacy.STRATEGY_ID = os.getenv("BACKTEST_STRATEGY_ID", "nautilus_target_model_predictions")
    legacy.build_engine = build_engine
    legacy.main()


if __name__ == "__main__":
    main()
