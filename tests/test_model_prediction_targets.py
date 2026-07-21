from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.identifiers import InstrumentId

from strategies.model_prediction_targets import TargetModelPredictionsStrategy


class ModelPredictionTargetsTest(unittest.TestCase):
    def _make_exit_strategy_stub(
        self,
        *,
        active_positions: dict[str, dict],
        today_open: dict[str, float],
        last_close: dict[str, float],
    ):
        class ExitStrategyStub:
            _prepare_model_exits = TargetModelPredictionsStrategy._prepare_model_exits
            _exit_price_with_source = TargetModelPredictionsStrategy._exit_price_with_source
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _log_missing_exit_open_price = TargetModelPredictionsStrategy._log_missing_exit_open_price
            _update_trailing_state = TargetModelPredictionsStrategy._update_trailing_state
            _record_signal = TargetModelPredictionsStrategy._record_signal

        strategy = ExitStrategyStub()
        strategy.config = SimpleNamespace(
            stop_loss=0.05,
            trailing_take_profit=0.0,
            trailing_take_profit_start=0.0,
        )
        strategy.log = MagicMock()
        strategy._active_positions = active_positions
        strategy._today_open = today_open
        strategy._last_close = last_close
        strategy._stock_by_instrument = {"000001.SZ.QMT": "000001.SZ"}
        strategy._current_quantity = MagicMock(return_value=100)
        strategy.signal_events = []
        return strategy

    def _make_target_plan_strategy_stub(
        self,
        *,
        active_positions: dict[str, dict],
        today_open: dict[str, float],
        last_close: dict[str, float],
    ):
        class TargetPlanStrategyStub:
            compute_daily_target_plan = TargetModelPredictionsStrategy.compute_daily_target_plan
            _resolve_signal_date = TargetModelPredictionsStrategy._resolve_signal_date
            _prepare_model_entries = TargetModelPredictionsStrategy._prepare_model_entries
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _log_missing_new_entry_open_price = TargetModelPredictionsStrategy._log_missing_new_entry_open_price
            _record_signal = TargetModelPredictionsStrategy._record_signal
            _trim_active_positions = TargetModelPredictionsStrategy._trim_active_positions

        strategy = TargetPlanStrategyStub()
        instrument_id = InstrumentId.from_str("000001.SZ.QMT")
        signal_date = date(2026, 7, 7)
        strategy.config = SimpleNamespace(holding_days=10, max_positions=3)
        strategy.log = MagicMock()
        strategy._seed_active_positions_from_portfolio = MagicMock()
        strategy._prepare_model_exits = MagicMock()
        strategy._entry_skip_reason = MagicMock(return_value=None)
        strategy._target_plan = MagicMock(return_value=SimpleNamespace(target_qty={}, reason="test"))
        strategy._signals_by_date = {
            signal_date: [
                {
                    "date": signal_date,
                    "stock_code": "000001.SZ",
                    "score": 0.9,
                    "rank": 1,
                },
            ],
        }
        strategy._trading_dates = [signal_date, date(2026, 7, 8)]
        strategy._rebalance_start_date = signal_date
        strategy._instrument_by_stock = {"000001.SZ": instrument_id}
        strategy._stock_by_instrument = {str(instrument_id): "000001.SZ"}
        strategy._active_positions = active_positions
        strategy._last_close = last_close
        strategy._today_open = today_open
        strategy.signal_events = []
        return strategy

    def test_compute_target_plan_skips_new_signal_without_open_price(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_target_plan_strategy_stub(
            active_positions={},
            today_open={},
            last_close={instrument_id: 10.0},
        )

        strategy.compute_daily_target_plan(date(2026, 7, 8))

        self.assertEqual(strategy._active_positions, {})
        strategy.log.warning.assert_called_once()
        warning = strategy.log.warning.call_args.args[0]
        self.assertIn("missing open price", warning)
        self.assertIn(instrument_id, warning)
        strategy._target_plan.assert_called_once_with(date(2026, 7, 8), date(2026, 7, 7))

    def test_compute_target_plan_keeps_existing_position_without_open_price(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_target_plan_strategy_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 9.5,
                    "high_price": 10.0,
                    "last_signal_date": date(2026, 7, 1),
                    "score": 0.1,
                },
            },
            today_open={},
            last_close={instrument_id: 10.0},
        )

        strategy.compute_daily_target_plan(date(2026, 7, 8))

        self.assertEqual(strategy._active_positions[instrument_id]["score"], 0.9)
        self.assertEqual(strategy._active_positions[instrument_id]["last_signal_date"], date(2026, 7, 7))
        strategy.log.warning.assert_not_called()
        strategy._target_plan.assert_called_once_with(date(2026, 7, 8), date(2026, 7, 7))

    def test_prepare_model_exits_uses_open_price_for_stop(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_exit_strategy_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "last_signal_date": date(2026, 7, 1),
                    "score": 0.2,
                },
            },
            today_open={instrument_id: 9.4},
            last_close={instrument_id: 10.0},
        )

        strategy._prepare_model_exits(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            target_ids={instrument_id},
            is_rebalance=False,
        )

        self.assertEqual(strategy._active_positions, {})
        self.assertEqual(strategy.signal_events[0].signal_name, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].extra["open_price"], 9.4)
        self.assertEqual(strategy.signal_events[0].extra["price_source"], "open")
        strategy.log.warning.assert_not_called()

    def test_prepare_model_exits_falls_back_to_last_close_with_warning(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_exit_strategy_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "last_signal_date": date(2026, 7, 1),
                    "score": 0.2,
                },
            },
            today_open={},
            last_close={instrument_id: 9.4},
        )

        strategy._prepare_model_exits(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            target_ids={instrument_id},
            is_rebalance=False,
        )

        self.assertEqual(strategy._active_positions, {})
        self.assertEqual(strategy.signal_events[0].signal_name, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].extra["open_price"], 9.4)
        self.assertEqual(strategy.signal_events[0].extra["price_source"], "prev_close")
        strategy.log.warning.assert_called_once()
        warning = strategy.log.warning.call_args.args[0]
        self.assertIn("missing open price", warning)
        self.assertIn(instrument_id, warning)


if __name__ == "__main__":
    unittest.main()
