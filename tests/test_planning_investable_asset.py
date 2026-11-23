from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import patch

from msgspec.structs import replace

from strategies.model_target_planners import RiskManagerModelTargetPlanner
from tests.test_opening_auction import replay_fixture, timestamp


DAY = date(2026, 7, 8)
A, B = "000001.SZ.QMT", "000002.SZ.QMT"


class PlanningInvestableAssetTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, _, _ = replay_fixture(
            starting_cash=100000,
            transform_config=lambda config: replace(
                config, target_cash_buffer_percent=0.12,
                trailing_take_profit=0.06, trailing_take_profit_start=0.15,
            ),
        )
        self.strategy.clock.set_time(timestamp("2026-07-08 10:00:00"))
        self.strategy._roll_trading_day(DAY)
        self.strategy._active_positions[A] = {
            "entry_price": 11, "high_price": 11, "entry_date": date(2026, 7, 7),
        }
        self.planner = RiskManagerModelTargetPlanner(
            "http://risk-manager.test", "cn_a_basic_constraints_integer_lots", "test",
            "backtest", log=self.strategy.log,
        )
        self.addCleanup(self.engine.dispose)

    def request(self, *, stage="continuous", special_account=False, quantities=None, snapshot=None):
        if snapshot is None:
            snapshot = {
                A: {"last_price": 9.4, "open": 9.4, "last_close": 10},
                B: {"last_price": 20, "open": 20, "last_close": 20},
            }
        context = self.strategy.planning_context_builder.build(DAY, stage=stage, snapshot=snapshot)
        quantities = quantities if quantities is not None else {A: 5000, B: 2500}
        with (
            patch.object(self.strategy, "_held_instrument_ids", return_value=set(quantities)),
            patch.object(self.strategy, "_current_quantity",
                         side_effect=lambda instrument: Decimal(quantities.get(str(instrument), 0))),
            patch.object(self.strategy, "planning_sellable_quantity",
                         side_effect=lambda instrument, day: quantities[instrument]
                         - int(self.strategy.planning_today_fills(instrument, day)[0])),
            patch.object(self.strategy, "_portfolio_value", return_value=Decimal("100000")),
            patch.object(self.strategy, "_account_id_equals", return_value=special_account),
        ):
            return self.strategy._target_planning_request(DAY, date(2026, 7, 7), planning_context=context)

    def test_fully_sellable_stop_does_not_reduce_sizing_basis_in_any_stage(self):
        for stage in ("opening_auction", "pre_open", "continuous"):
            with self.subTest(stage=stage):
                request, exits = self.request(stage=stage)
                self.assertEqual(exits[A].target_quantity, 0)
                self.assertEqual([h.instrument_id for h in request.current_holdings], [B])
                self.assertAlmostEqual(request.investable_asset, 97000 * 1.005)
                self.assertAlmostEqual(self.planner._payload(request, "test")["total_asset"], 97000 * 1.005)

    def test_partial_stop_uses_full_position_instead_of_locked_remainder(self):
        self.strategy.restore_today_fills(DAY, [{
            "trade_id": "buy1", "instrument_id": A, "side": "buy", "quantity": 4000,
        }])
        request, exits = self.request()
        holding = next(h for h in request.current_holdings if h.instrument_id == A)
        self.assertEqual(exits[A].target_quantity, 4000)
        self.assertEqual(holding.quantity, 4000)
        self.assertFalse(holding.can_buy)
        self.assertFalse(holding.can_sell)
        self.assertAlmostEqual(request.investable_asset, 97000 * 1.005)

    def test_special_account_uses_full_holdings_value(self):
        request, _ = self.request(special_account=True)
        self.assertAlmostEqual(request.investable_asset, 97000 * 1.01)

    def test_trailing_take_profit_does_not_reduce_sizing_basis(self):
        self.strategy._active_positions[A]["entry_price"] = 8
        with (
            patch.object(self.strategy, "_current_adjustment_factor", return_value=1.0),
            patch.object(self.strategy, "_adjusted_entry_cost", return_value=8.0),
        ):
            request, exits = self.request()
        self.assertEqual(exits[A].reason, "trailing_take_profit_triggered")
        self.assertEqual([h.instrument_id for h in request.current_holdings], [B])
        self.assertAlmostEqual(request.investable_asset, 97000 * 1.005)

    def test_missing_held_position_price_does_not_silently_reduce_budget(self):
        with self.assertRaisesRegex(ValueError, f"valid price to value holding {A}"):
            self.request(snapshot={})

    def test_buffer_is_kept_when_full_holdings_value_is_below_budget(self):
        request, _ = self.request(quantities={A: 3000, B: 2000})
        self.assertEqual(request.total_asset, 100000)
        self.assertAlmostEqual(request.investable_asset, 88000)

    def test_empty_holdings_keep_normal_budget(self):
        request, _ = self.request(quantities={})
        self.assertEqual(request.current_holdings, [])
        self.assertAlmostEqual(request.investable_asset, 88000)
