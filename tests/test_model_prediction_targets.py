from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.enums import MarketStatusAction
from nautilus_trader.model.identifiers import InstrumentId

from strategies.model_prediction_targets import TargetModelPredictionsStrategy
from strategies.model_target_planners import CurrentHolding
from strategies.model_target_planners import ModelTargetCandidate
from strategies.model_target_planners import ModelTargetPlan
from strategies.model_target_planners import ModelTargetPlanningRequest
from strategies.model_target_planners import TargetContext
from strategies.model_target_planners import TargetInfo
from strategies.target_quantities import TickSnapshot


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
            _stock_code_for_instrument = TargetModelPredictionsStrategy._stock_code_for_instrument
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
        strategy._instrument_by_stock = {"000001.SZ": InstrumentId.from_str("000001.SZ.QMT")}
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


class BuildCurrentHoldingsTest(unittest.TestCase):
    def _make_stub(self):
        class HoldingStub:
            _build_current_holdings = TargetModelPredictionsStrategy._build_current_holdings
            _recent_target_dates = TargetModelPredictionsStrategy._recent_target_dates
            _recent_target_cutoff_date = TargetModelPredictionsStrategy._recent_target_cutoff_date
            _recent_holding_days = TargetModelPredictionsStrategy._recent_holding_days
            _stock_code_for_instrument = TargetModelPredictionsStrategy._stock_code_for_instrument
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _is_suspended_status = TargetModelPredictionsStrategy._is_suspended_status

        strategy = HoldingStub()
        strategy.log = MagicMock()
        strategy._stock_by_instrument = {}
        strategy._instrument_by_stock = {}
        strategy._recent_target_loader = None
        strategy._market_status = {}
        strategy._last_close = {}
        strategy._suspended_by_date = {}
        strategy._trading_dates = [date(2026, 7, 22), date(2026, 7, 23)]
        strategy._active_positions = {
            "000157.SZ.QMT": {"entry_date": date(2026, 7, 22)},
        }
        strategy._today_open = {"000157.SZ.QMT": 10.1}
        strategy._held_instrument_ids = MagicMock(return_value={"000157.SZ.QMT"})
        strategy._current_quantity = MagicMock(return_value=100)
        strategy._holding_exclusion = MagicMock(return_value=None)
        strategy._log_missing_new_entry_open_price = MagicMock()
        return strategy

    def test_holding_stock_code_comes_from_nautilus_instrument_symbol(self) -> None:
        strategy = self._make_stub()
        open_prices: dict[str, float] = {}

        holdings = strategy._build_current_holdings(
            date(2026, 7, 23),
            date(2026, 7, 22),
            open_prices,
        )

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].instrument_id, "000157.SZ.QMT")
        self.assertEqual(holdings[0].stock_code, "000157.SZ")
        self.assertEqual(strategy._stock_by_instrument["000157.SZ.QMT"], "000157.SZ")
        self.assertEqual(str(strategy._instrument_by_stock["000157.SZ"]), "000157.SZ.QMT")
        self.assertEqual(open_prices["000157.SZ.QMT"], 10.1)
        strategy.log.warning.assert_not_called()

    def test_suspended_holding_is_frozen_priced_at_last_close(self) -> None:
        strategy = self._make_stub()
        # No open price today (suspended stock has none); status is SUSPEND and a
        # previous close is available.
        strategy._today_open = {}
        strategy._market_status = {
            "000157.SZ.QMT": TickSnapshot(market_status=MarketStatusAction.SUSPEND),
        }
        strategy._last_close = {"000157.SZ.QMT": 9.5}
        strategy._holding_exclusion = MagicMock()
        open_prices: dict[str, float] = {}

        holdings = strategy._build_current_holdings(
            date(2026, 7, 23),
            date(2026, 7, 22),
            open_prices,
        )

        self.assertEqual(len(holdings), 1)
        holding = holdings[0]
        self.assertEqual(holding.instrument_id, "000157.SZ.QMT")
        # Priced at last_close, held quantity kept, recency zeroed to freeze it.
        self.assertEqual(holding.price, 9.5)
        self.assertEqual(holding.quantity, 100)
        self.assertEqual(holding.recent_holding_days, 0)
        self.assertFalse(holding.can_buy)
        self.assertFalse(holding.can_sell)
        self.assertEqual(open_prices["000157.SZ.QMT"], 9.5)
        # Frozen: the exit/exclusion logic is bypassed entirely.
        strategy._holding_exclusion.assert_not_called()
        # A warning documents the suspended special-handling.
        strategy.log.warning.assert_called()

    def test_suspended_holding_without_last_close_is_skipped(self) -> None:
        strategy = self._make_stub()
        strategy._today_open = {}
        strategy._market_status = {
            "000157.SZ.QMT": TickSnapshot(market_status=MarketStatusAction.SUSPEND),
        }
        strategy._last_close = {}
        strategy._holding_exclusion = MagicMock()
        open_prices: dict[str, float] = {}

        holdings = strategy._build_current_holdings(
            date(2026, 7, 23),
            date(2026, 7, 22),
            open_prices,
        )

        self.assertEqual(holdings, [])
        strategy.log.warning.assert_called()

    def test_calendar_suspended_holding_is_frozen_and_non_tradable(self) -> None:
        strategy = self._make_stub()
        strategy._today_open = {}
        strategy._last_close = {"000157.SZ.QMT": 9.5}
        strategy._suspended_by_date = {date(2026, 7, 23): {"000157.SZ"}}
        open_prices: dict[str, float] = {}

        holdings = strategy._build_current_holdings(
            date(2026, 7, 23),
            date(2026, 7, 22),
            open_prices,
        )

        self.assertEqual(len(holdings), 1)
        self.assertFalse(holdings[0].can_buy)
        self.assertFalse(holdings[0].can_sell)
        strategy._holding_exclusion.assert_not_called()

    def test_recent_target_cutoff_window_defaults_to_90(self) -> None:
        strategy = self._make_stub()
        # 120 prior trading days: the default window (90) selects the 90th-prior day.
        strategy._trading_dates = [date(2020, 1, 1)] + [
            date(2026, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(120)
        ]
        trading_date = date(2027, 1, 1)
        prior = sorted(d for d in strategy._trading_dates if d < trading_date)
        cutoff = strategy._recent_target_cutoff_date(trading_date)
        self.assertEqual(cutoff, prior[-90])


class ComputeDailyTargetPlanTest(unittest.TestCase):
    def _make_stub(self, *, signals: list[dict]):
        class PlanStub:
            compute_daily_target_plan = TargetModelPredictionsStrategy.compute_daily_target_plan
            _has_expected_signal_date = TargetModelPredictionsStrategy._has_expected_signal_date
            _resolve_signal_date = TargetModelPredictionsStrategy._resolve_signal_date

        strategy = PlanStub()
        signal_date = date(2026, 7, 7)
        strategy.log = MagicMock()
        strategy._seed_active_positions_from_portfolio = MagicMock()
        strategy._target_plan = MagicMock(
            return_value=ModelTargetPlan(
                trading_date=date(2026, 7, 8),
                signal_date=signal_date,
                targets=[],
                reason="test",
            ),
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

    def test_skips_plan_and_reports_when_signal_date_is_not_previous_trading_date(self) -> None:
        strategy = self._make_stub(signals=[])
        strategy._signals_by_date = {date(2026, 7, 6): []}
        strategy._signal_date_alert_reporter = MagicMock(return_value=True)

        result = strategy.compute_daily_target_plan(date(2026, 7, 8))

        self.assertIsNone(result)
        strategy._seed_active_positions_from_portfolio.assert_not_called()
        strategy._target_plan.assert_not_called()
        strategy.log.error.assert_called_once()
        strategy._signal_date_alert_reporter.assert_called_once_with(
            "TARGET-MODEL-SIGNAL-DATE-MISMATCH",
            "failed",
            {
                "trading_date": date(2026, 7, 8),
                "expected_signal_date": date(2026, 7, 7),
                "resolved_signal_date": date(2026, 7, 6),
            },
        )


class AnnotatePlanTest(unittest.TestCase):
    def test_current_holding_quantity_is_stamped_on_target_context(self) -> None:
        class AnnotateStub:
            _annotate_plan = TargetModelPredictionsStrategy._annotate_plan
            _open_price_with_source = TargetModelPredictionsStrategy._open_price_with_source
            _market_status_for = TargetModelPredictionsStrategy._market_status_for

        strategy = AnnotateStub()
        strategy._today_open = {"000001.SZ.QMT": 10.1}
        strategy._last_close = {"000001.SZ.QMT": 10.0}
        strategy._market_status = {}
        plan = ModelTargetPlan(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            targets=[
                TargetInfo(
                    stock_code="000001.SZ",
                    weight=0.2,
                    quantity=2000,
                    target_context=TargetContext(),
                    target_version=None,
                    instrument_id="000001.SZ.QMT",
                    is_locked=True,
                ),
            ],
            reason="risk_manager_optimize",
        )
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 8),
            signal_date=date(2026, 7, 7),
            active_instrument_ids=["000001.SZ.QMT"],
            candidates=[
                ModelTargetCandidate(
                    "000001.SZ.QMT",
                    "000001.SZ",
                    0.8,
                    open_price=10.1,
                    expected_return=0.03,
                ),
            ],
            current_holdings=[
                CurrentHolding(
                    "000001.SZ.QMT",
                    "000001.SZ",
                    quantity=1200,
                    price=10.1,
                    recent_target_date=date(2026, 7, 1),
                    recent_holding_days=5,
                ),
            ],
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
            total_asset=1_000_000,
            investable_asset=950_000,
            open_prices={"000001.SZ.QMT": 10.1},
        )

        annotated = strategy._annotate_plan(plan, request)

        target_context = annotated.targets[0].target_context
        self.assertEqual(target_context.current_qty, 1200)
        self.assertEqual(target_context.price_source, "open")
        self.assertEqual(target_context.recent_target_date, date(2026, 7, 1))


class BuildCandidatesTest(unittest.TestCase):
    def _make_stub(self, *, signals: list[dict], today_open: dict[str, float]):
        class CandidateStub:
            _build_candidates = TargetModelPredictionsStrategy._build_candidates
            _entry_skip_reason = TargetModelPredictionsStrategy._entry_skip_reason
            _name_skip_reason = MagicMock(return_value=None)
            _today_open_price = TargetModelPredictionsStrategy._today_open_price
            _open_price_with_source = TargetModelPredictionsStrategy._open_price_with_source
            _log_missing_new_entry_open_price = (
                TargetModelPredictionsStrategy._log_missing_new_entry_open_price
            )
            _record_signal = TargetModelPredictionsStrategy._record_signal
            _is_suspended_status = TargetModelPredictionsStrategy._is_suspended_status

        strategy = CandidateStub()
        signal_date = date(2026, 7, 7)
        instrument_id = InstrumentId.from_str("000001.SZ.QMT")
        strategy.log = MagicMock()
        strategy._market_status = {}
        strategy._suspended_by_date = {}
        strategy._st_by_date = {}
        strategy._listed_dates = {}
        strategy.config = MagicMock(min_listed_days=0)
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

    def test_suspended_signal_is_filtered_out_of_candidates(self) -> None:
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
            today_open={"000001.SZ.QMT": 10.1},
        )
        strategy._market_status = {
            instrument_id: TickSnapshot(market_status=MarketStatusAction.SUSPEND),
        }
        open_prices: dict[str, float] = {}

        candidates = strategy._build_candidates(date(2026, 7, 8), signal_date, open_prices)

        self.assertEqual(candidates, [])
        self.assertEqual(strategy.signal_events[0].signal_name, "entry_filtered")
        self.assertEqual(strategy.signal_events[0].extra["reason"], "suspended")


if __name__ == "__main__":
    unittest.main()
