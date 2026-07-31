from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from backtests.data_providers import PredictionDataBundle
from backtests.data_providers import PredictionSignal
from backtests.result_writers.mysql import MySQLResultWriter
from backtests.target_model_predictions.run_backtest import (
    BacktestTargetModelPredictionsStrategy,
)
from backtests.target_model_predictions.run_backtest import DailyPlanningEvent
from backtests.target_model_predictions.run_backtest import EXECUTION_BAR_TIME
from backtests.target_model_predictions.run_backtest import PLANNING_CLIENT_ID
from backtests.target_model_predictions.run_backtest import PLANNING_TIME
from backtests.target_model_predictions.run_backtest import bar_trading_date
from backtests.target_model_predictions.run_backtest import build_bar_type
from backtests.target_model_predictions.run_backtest import build_engine
from backtests.target_model_predictions.run_backtest import exchange_timestamp_ns
from backtests.target_model_predictions.run_backtest import prepare_backtest_event_data
from backtests.target_model_predictions.run_backtest import qmt_symbol
from backtests.target_model_predictions.run_backtest import order_records
from backtests.target_model_predictions.run_backtest import strategy_order_frame
from backtests.target_model_predictions.run_backtest import strategy_target_frame
from backtests.target_model_predictions.run_backtest import target_records
from nautilus_trader.adapters.qmt.common import parse_equity
from nautilus_trader.model.data import Bar
from strategies.model_target_planners import ModelTargetPlan
from strategies.model_target_planners import TargetContext
from strategies.model_target_planners import TargetInfo
from strategies.model_prediction_targets import TargetModelPredictionsStrategy


class BacktestPlanningEventTest(unittest.TestCase):
    timezone_name = "Asia/Shanghai"

    @staticmethod
    def _bundle(
        trading_dates: list[date],
        suspended_by_date: dict[date, set[str]] | None = None,
    ) -> PredictionDataBundle:
        return PredictionDataBundle(
            signals_by_date={},
            universe=["000001.SZ", "000002.SZ"],
            trading_dates=trading_dates,
            listed_dates={},
            st_by_date={},
            suspended_by_date=suspended_by_date or {},
            instrument_names={},
            prediction_rows=0,
            selected_rows=0,
        )

    def _instrument(self, stock_code: str, timestamp_ns: int):
        return parse_equity(
            symbol=qmt_symbol(stock_code),
            fields={"name": stock_code, "source": "test"},
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        )

    def _bar(
        self, stock_code: str, trading_date: date, open_price: float, close_price: float
    ) -> Bar:
        timestamp_ns = exchange_timestamp_ns(
            trading_date, "00:00:00", self.timezone_name
        )
        instrument = self._instrument(stock_code, timestamp_ns)
        bar_type = build_bar_type(stock_code)
        return Bar(
            bar_type=bar_type,
            open=instrument.make_price(open_price),
            high=instrument.make_price(max(open_price, close_price) + 1),
            low=instrument.make_price(min(open_price, close_price) - 1),
            close=instrument.make_price(close_price),
            volume=instrument.make_qty(10000),
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        )

    def test_planning_event_applies_snapshot_then_processes_once(self) -> None:
        trading_date = date(2026, 7, 8)
        timestamp_ns = exchange_timestamp_ns(
            trading_date, PLANNING_TIME, self.timezone_name
        )
        event = DailyPlanningEvent(
            trading_date=trading_date,
            full_tick_snapshot={"000001.SZ.QMT": {"open": 10.0}},
            previous_closes={"000001.SZ.QMT": 9.8},
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        )
        strategy = BacktestTargetModelPredictionsStrategy.__new__(
            BacktestTargetModelPredictionsStrategy,
        )
        strategy._roll_trading_day = MagicMock()
        strategy.refresh_target_instruments = MagicMock()
        strategy.apply_full_tick_snapshot = MagicMock()
        strategy._process_trading_day_once = MagicMock()

        strategy.on_data(event)

        strategy.apply_full_tick_snapshot.assert_called_once_with(
            event.full_tick_snapshot,
            "backtest_planning",
        )
        strategy._roll_trading_day.assert_called_once_with(trading_date)
        strategy.refresh_target_instruments.assert_called_once_with(
            instrument_ids=[],
            bar_types={},
            last_closes=event.previous_closes,
            subscribe_new_bars=False,
        )
        strategy._process_trading_day_once.assert_called_once_with(
            trading_date,
            "planning_event",
        )

    def test_strategy_subscribes_to_the_planning_data_client(self) -> None:
        strategy = BacktestTargetModelPredictionsStrategy.__new__(
            BacktestTargetModelPredictionsStrategy,
        )
        strategy.subscribe_data = MagicMock()

        with patch.object(TargetModelPredictionsStrategy, "on_start"):
            strategy.on_start()

        args = strategy.subscribe_data.call_args.args
        kwargs = strategy.subscribe_data.call_args.kwargs
        self.assertIs(args[0].type, DailyPlanningEvent)
        self.assertEqual(kwargs["client_id"], PLANNING_CLIENT_ID)

    def test_daily_snapshot_precedes_execution_bars_and_contains_all_opens(
        self,
    ) -> None:
        first_date = date(2026, 7, 7)
        second_date = date(2026, 7, 8)
        bars_by_stock = {
            "000001.SZ": [
                self._bar("000001.SZ", first_date, 10.0, 10.5),
                self._bar("000001.SZ", second_date, 11.0, 11.5),
            ],
            "000002.SZ": [
                self._bar("000002.SZ", first_date, 20.0, 20.5),
                self._bar("000002.SZ", second_date, 21.0, 21.5),
            ],
        }
        instruments = {
            stock_code: self._instrument(stock_code, bars[0].ts_init)
            for stock_code, bars in bars_by_stock.items()
        }
        args = SimpleNamespace(
            start=first_date.isoformat(),
            end=second_date.isoformat(),
            exchange_timezone=self.timezone_name,
        )

        planning, heartbeats, execution_bars = prepare_backtest_event_data(
            args=args,
            bundle=self._bundle([first_date, second_date]),
            bars_by_stock=bars_by_stock,
            instruments_by_stock=instruments,
        )

        self.assertEqual(len(planning), 2)
        self.assertEqual(len(heartbeats), 2)
        second_event = planning[1].data
        self.assertEqual(
            second_event.full_tick_snapshot["000001.SZ.QMT"]["open"],
            11.0,
        )
        self.assertEqual(
            second_event.full_tick_snapshot["000002.SZ.QMT"]["open"],
            21.0,
        )
        self.assertNotIn("high", second_event.full_tick_snapshot["000001.SZ.QMT"])
        self.assertNotIn("low", second_event.full_tick_snapshot["000001.SZ.QMT"])
        self.assertLess(heartbeats[1].ts_init, second_event.ts_init)
        expected_execution_ts = exchange_timestamp_ns(
            second_date,
            EXECUTION_BAR_TIME,
            self.timezone_name,
        )
        self.assertEqual(execution_bars["000001.SZ"][1].ts_init, expected_execution_ts)
        self.assertGreater(execution_bars["000001.SZ"][1].ts_init, second_event.ts_init)

    def test_suspended_instrument_uses_status_without_an_execution_bar(self) -> None:
        first_date = date(2026, 7, 7)
        second_date = date(2026, 7, 8)
        bars_by_stock = {
            "000001.SZ": [
                self._bar("000001.SZ", first_date, 10.0, 10.5),
                self._bar("000001.SZ", second_date, 11.0, 11.5),
            ],
            "000002.SZ": [self._bar("000002.SZ", first_date, 20.0, 20.5)],
        }
        instruments = {
            stock_code: self._instrument(stock_code, bars[0].ts_init)
            for stock_code, bars in bars_by_stock.items()
        }
        args = SimpleNamespace(
            start=first_date.isoformat(),
            end=second_date.isoformat(),
            exchange_timezone=self.timezone_name,
        )

        planning, _, execution_bars = prepare_backtest_event_data(
            args=args,
            bundle=self._bundle(
                [first_date, second_date],
                suspended_by_date={second_date: {"000002.SZ"}},
            ),
            bars_by_stock=bars_by_stock,
            instruments_by_stock=instruments,
        )

        suspended = planning[1].data.full_tick_snapshot["000002.SZ.QMT"]
        self.assertEqual(suspended["open"], 0.0)
        self.assertEqual(suspended["last_close"], 20.5)
        self.assertEqual(suspended["open_int"], 1)
        self.assertEqual(
            planning[1].data.previous_closes["000002.SZ.QMT"],
            20.5,
        )
        self.assertEqual(len(execution_bars["000002.SZ"]), 1)
        self.assertEqual(
            bar_trading_date(execution_bars["000002.SZ"][0], self.timezone_name),
            first_date,
        )

    def test_next_planning_event_receives_position_filled_by_prior_execution_bar(
        self,
    ) -> None:
        signal_date = date(2026, 7, 7)
        first_trade_date = date(2026, 7, 8)
        second_trade_date = date(2026, 7, 9)
        bars_by_stock = {
            "000001.SZ": [
                self._bar("000001.SZ", first_trade_date, 10.0, 10.5),
                self._bar("000001.SZ", second_trade_date, 11.0, 11.5),
            ],
        }
        args = SimpleNamespace(
            start=signal_date.isoformat(),
            end=second_trade_date.isoformat(),
            exchange_timezone=self.timezone_name,
            trader_id="BACKTESTER-001",
            log_level="ERROR",
            starting_cash=Decimal("1000000"),
            max_positions=50,
            max_position_percent=0.03,
            holding_days=10,
            stop_loss=0.05,
            trailing_take_profit=0.0,
            trailing_take_profit_start=0.0,
            min_listed_days=0,
        )
        signals = {
            signal_date: [PredictionSignal(signal_date, "000001.SZ", 0.9, 1, 0.02)],
            first_trade_date: [
                PredictionSignal(first_trade_date, "000001.SZ", 0.8, 1, 0.01),
            ],
        }
        bundle = PredictionDataBundle(
            signals_by_date=signals,
            universe=["000001.SZ"],
            trading_dates=[signal_date, first_trade_date, second_trade_date],
            listed_dates={},
            st_by_date={},
            suspended_by_date={},
            instrument_names={},
            prediction_rows=2,
            selected_rows=2,
        )
        bar_types = {"000001.SZ": build_bar_type("000001.SZ")}
        with patch.dict(
            "os.environ",
            {
                "TARGET_MODEL_WEIGHT_PLANNER": "risk_manager",
                "RISK_MANAGER_BASE_URL": "http://127.0.0.1:1",
                "RISK_MANAGER_RISK_MODEL_ID": "test",
            },
        ):
            engine, strategy, _ = build_engine(args, bundle, bar_types, bars_by_stock)

        class CapturingPlanner:
            def __init__(self) -> None:
                self.requests = []

            def plan(self, request):
                self.requests.append(request)
                candidate = request.candidates[0]
                return ModelTargetPlan(
                    trading_date=request.trading_date,
                    signal_date=request.signal_date,
                    targets=[
                        TargetInfo(
                            stock_code=candidate.stock_code,
                            weight=None,
                            quantity=100,
                            target_context=TargetContext(),
                            target_version=None,
                            instrument_id=candidate.instrument_id,
                            is_locked=False,
                        ),
                    ],
                    reason="test_plan",
                )

        planner = CapturingPlanner()
        strategy._target_planner = planner
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(planner.requests[0].current_holdings, [])
            self.assertEqual(len(planner.requests[1].current_holdings), 1)
            self.assertEqual(planner.requests[1].current_holdings[0].quantity, 100)
            self.assertFalse(strategy.config.subscribe_bars)
            self.assertFalse(strategy.config.process_targets_on_timer)
            self.assertEqual(strategy.config.unfilled_timeout_secs, 0)
        finally:
            engine.dispose()

    def test_quantity_target_and_order_events_export_without_weight_fields(self) -> None:
        trading_date = date(2026, 7, 8)
        strategy = SimpleNamespace(
            target_events=[
                SimpleNamespace(
                    target_id="target-1",
                    target_date=trading_date,
                    execute_date=trading_date,
                    instrument_id="000001.SZ.QMT",
                    target_qty=Decimal("300"),
                    current_qty=Decimal("100"),
                    delta_qty=Decimal("200"),
                    reason="risk_manager_optimize",
                    extra={"target_version": "v1"},
                ),
            ],
            order_events=[
                SimpleNamespace(
                    order_id="order-1",
                    trading_date=trading_date,
                    instrument_id="000001.SZ.QMT",
                    side="BUY",
                    quantity=200,
                    target_qty=Decimal("300"),
                    status="submitted",
                    reason=None,
                    extra={"target_version": "v1"},
                ),
            ],
        )

        target = target_records("experiment-1", strategy)[0]
        order = order_records("experiment-1", strategy)[0]
        target_frame = strategy_target_frame(strategy)
        order_frame = strategy_order_frame(strategy)

        self.assertEqual(target.target_qty, 300)
        self.assertEqual(target.current_qty, 100)
        self.assertEqual(target.delta_qty, 200)
        self.assertIsNone(target.target_weight)
        self.assertEqual(order.order_type, "target_quantity")
        self.assertEqual(order.target_qty, 300)
        self.assertIsNone(order.target_weight)
        self.assertEqual(target_frame.loc[0, "target_qty"], Decimal("300"))
        self.assertEqual(order_frame.loc[0, "target_qty"], Decimal("300"))

        writer = object.__new__(MySQLResultWriter)
        writer._upsert_many = MagicMock()
        writer.write_target_portfolios([target])
        target_row = writer._upsert_many.call_args.args[1][0]
        self.assertEqual(target_row["target_qty"], 300)
        self.assertEqual(target_row["current_qty"], 100)
        self.assertEqual(target_row["delta_qty"], 200)

        writer.write_orders([order])
        order_row = writer._upsert_many.call_args.args[1][0]
        self.assertEqual(order_row["target_qty"], 300)

if __name__ == "__main__":
    unittest.main()
