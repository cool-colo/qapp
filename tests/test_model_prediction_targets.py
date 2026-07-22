from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.identifiers import InstrumentId

from strategies.model_prediction_targets import TargetModelPredictionsStrategy


class HoldingExclusionTest(unittest.TestCase):
    """
    The stop-loss / trailing-take-profit business logic is unchanged; it now runs as an
    *exclusion* filter (``_holding_exclusion``) that keeps a held position out of the
    risk-manager ``current_holdings`` so the optimizer unwinds it, rather than mutating
    ``_active_positions`` in place.
    """

    def _make_stub(
        self,
        *,
        active_positions: dict[str, dict],
        today_open: dict[str, float],
        last_close: dict[str, float],
        st_by_date: dict[date, set[str]] | None = None,
        suspended_by_date: dict[date, set[str]] | None = None,
    ):
        class HoldingStub:
            _holding_exclusion = TargetModelPredictionsStrategy._holding_exclusion
            _untradable_reason = TargetModelPredictionsStrategy._untradable_reason
            _exit_price_with_source = TargetModelPredictionsStrategy._exit_price_with_source
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _log_missing_exit_open_price = TargetModelPredictionsStrategy._log_missing_exit_open_price
            _update_trailing_state = TargetModelPredictionsStrategy._update_trailing_state
            _record_signal = TargetModelPredictionsStrategy._record_signal

        strategy = HoldingStub()
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
        strategy._st_by_date = st_by_date or {}
        strategy._suspended_by_date = suspended_by_date or {}
        strategy.signal_events = []
        return strategy

    def test_stop_loss_excludes_and_records_using_open_price(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "score": 0.2,
                },
            },
            today_open={instrument_id: 9.4},
            last_close={instrument_id: 10.0},
        )

        reason = strategy._holding_exclusion(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            instrument_id=instrument_id,
            exit_rank=1,
        )

        self.assertEqual(reason, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].signal_name, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].extra["open_price"], 9.4)
        self.assertEqual(strategy.signal_events[0].extra["price_source"], "open")
        strategy.log.warning.assert_not_called()

    def test_stop_loss_falls_back_to_last_close_with_warning(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "score": 0.2,
                },
            },
            today_open={},
            last_close={instrument_id: 9.4},
        )

        reason = strategy._holding_exclusion(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            instrument_id=instrument_id,
            exit_rank=1,
        )

        self.assertEqual(reason, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].signal_name, "stop_triggered")
        self.assertEqual(strategy.signal_events[0].extra["open_price"], 9.4)
        self.assertEqual(strategy.signal_events[0].extra["price_source"], "prev_close")
        strategy.log.warning.assert_called_once()
        warning = strategy.log.warning.call_args.args[0]
        self.assertIn("missing open price", warning)
        self.assertIn(instrument_id, warning)

    def test_healthy_holding_is_kept(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "score": 0.2,
                },
            },
            today_open={instrument_id: 10.2},
            last_close={instrument_id: 10.0},
        )

        reason = strategy._holding_exclusion(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            instrument_id=instrument_id,
            exit_rank=1,
        )

        self.assertIsNone(reason)
        self.assertEqual(strategy.signal_events, [])

    def test_suspended_holding_is_excluded(self) -> None:
        instrument_id = "000001.SZ.QMT"
        strategy = self._make_stub(
            active_positions={
                instrument_id: {
                    "entry_date": date(2026, 7, 1),
                    "entry_price": 10.0,
                    "high_price": 10.0,
                    "score": 0.2,
                },
            },
            today_open={instrument_id: 10.2},
            last_close={instrument_id: 10.0},
            suspended_by_date={date(2026, 7, 8): {"000001.SZ"}},
        )

        reason = strategy._holding_exclusion(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            instrument_id=instrument_id,
            exit_rank=1,
        )

        self.assertEqual(reason, "suspended")
        self.assertEqual(strategy.signal_events[0].signal_name, "suspended")


class ComputeDailyTargetPlanTest(unittest.TestCase):
    def _make_stub(self, *, signals: list[dict]):
        class PlanStub:
            compute_daily_target_plan = TargetModelPredictionsStrategy.compute_daily_target_plan
            _resolve_signal_date = TargetModelPredictionsStrategy._resolve_signal_date

        strategy = PlanStub()
        signal_date = date(2026, 7, 7)
        strategy.log = MagicMock()
        strategy._seed_active_positions_from_portfolio = MagicMock()
        strategy._target_plan = MagicMock(
            return_value=SimpleNamespace(target_qty={}, reason="test"),
        )
        strategy._signals_by_date = {signal_date: signals}
        strategy._trading_dates = [signal_date, date(2026, 7, 8)]
        strategy._active_positions = {}
        return strategy

    def test_computes_plan_for_resolved_signal_date(self) -> None:
        signal_date = date(2026, 7, 7)
        strategy = self._make_stub(
            signals=[
                {
                    "date": signal_date,
                    "stock_code": "000001.SZ",
                    "score": 0.9,
                    "rank": 1,
                    "pred_return_live": 0.03,
                },
            ],
        )

        strategy.compute_daily_target_plan(date(2026, 7, 8))

        strategy._seed_active_positions_from_portfolio.assert_called_once_with(date(2026, 7, 8))
        strategy._target_plan.assert_called_once_with(date(2026, 7, 8), signal_date)


class BuildCandidatesTest(unittest.TestCase):
    def _make_stub(self, *, signals: list[dict], today_open: dict[str, float]):
        class CandidateStub:
            _build_candidates = TargetModelPredictionsStrategy._build_candidates
            _entry_skip_reason = MagicMock(return_value=None)
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _open_price_with_source = TargetModelPredictionsStrategy._open_price_with_source
            _log_missing_new_entry_open_price = (
                TargetModelPredictionsStrategy._log_missing_new_entry_open_price
            )
            _record_signal = TargetModelPredictionsStrategy._record_signal

        strategy = CandidateStub()
        signal_date = date(2026, 7, 7)
        instrument_id = InstrumentId.from_str("000001.SZ.QMT")
        strategy.log = MagicMock()
        strategy._signals_by_date = {signal_date: signals}
        strategy._instrument_by_stock = {"000001.SZ": instrument_id}
        strategy._stock_by_instrument = {str(instrument_id): "000001.SZ"}
        strategy._today_open = today_open
        strategy._last_close = {str(instrument_id): 10.0}
        strategy.signal_events = []
        return strategy, str(instrument_id), signal_date

    def test_candidate_carries_expected_return(self) -> None:
        strategy, instrument_id, signal_date = self._make_stub(
            signals=[
                {
                    "date": date(2026, 7, 7),
                    "stock_code": "000001.SZ",
                    "score": 0.9,
                    "rank": 1,
                    "pred_return_live": 0.042,
                },
            ],
            today_open={"000001.SZ.QMT": 10.1},
        )
        open_prices: dict[str, float] = {}

        candidates = strategy._build_candidates(date(2026, 7, 8), signal_date, open_prices)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instrument_id, instrument_id)
        self.assertEqual(candidates[0].expected_return, 0.042)
        self.assertEqual(candidates[0].score, 0.9)
        self.assertEqual(open_prices[instrument_id], 10.1)
        self.assertEqual(strategy.signal_events[0].signal_name, "model_prediction_score")

    def test_signal_without_open_price_is_filtered(self) -> None:
        strategy, instrument_id, signal_date = self._make_stub(
            signals=[
                {
                    "date": date(2026, 7, 7),
                    "stock_code": "000001.SZ",
                    "score": 0.9,
                    "rank": 1,
                    "pred_return_live": 0.02,
                },
            ],
            today_open={},
        )
        open_prices: dict[str, float] = {}

        candidates = strategy._build_candidates(date(2026, 7, 8), signal_date, open_prices)

        self.assertEqual(candidates, [])
        self.assertEqual(strategy.signal_events[0].signal_name, "entry_filtered")
        self.assertEqual(strategy.signal_events[0].extra["reason"], "missing_open_price")
        strategy.log.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
