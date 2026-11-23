from __future__ import annotations

from tests.orchestration_helpers import auction_contexts

from tests.orchestration_helpers import committed_archive

import asyncio
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import json
import math
import threading
import warnings
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from backtests.result_writers.live_writer import LiveSnapshotWriter
from backtests.result_writers.live_control_writer import LiveControlWriter
from backtests.target_model_predictions.run_backtest import BacktestTargetModelPredictionsStrategy
from strategies.replan_triggers import ReplanTriggerTracker, price_limit_state
from strategies.model_target_planners.risk_manager import RiskManagerModelTargetPlanner
from strategies.replanning import PlanningAttempt, ReplanningBusyError, planning_json
from strategies.model_target_planners import ModelTargetPlanningContext, ModelTargetInstrumentContext
from strategies.model_target_planners import ModelTargetPlan, TargetInfo, TargetContext
from strategies.trading_control import SELL_REASON, SELL_ALL_REASON
from lives.control_server import LiveControlServer
from lives.snapshot_recorder import SnapshotRecorder, SnapshotRecorderConfig
from market_data import DailyStockData
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Equity
from tests.test_opening_auction import replay_fixture, timestamp
from tests.orchestration_helpers import evaluate_plan, start_runtime


DAY = date(2026, 7, 8)
A, B = "000001.SZ.QMT", "000002.SZ.QMT"


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, self.planner, _ = replay_fixture(auction_enabled=False)
        self.strategy.clock.set_time(timestamp("2026-07-08 14:30:00"))
        self.strategy._roll_trading_day(DAY)
        self.strategy.apply_full_tick_snapshot({
            A: {"last_price": 10, "open": 10, "last_close": 10},
            B: {"last_price": 20, "open": 20, "last_close": 20},
        }, "test")
        self.strategy._within_execution_window = MagicMock(return_value=True)
        self.strategy._within_cancel_window = MagicMock(return_value=True)
        self.strategy._schedule_convergence = MagicMock()
        self.strategy._refresh_order_book_depth_subscriptions = MagicMock()

    def tearDown(self):
        self.engine.dispose()

    def context(self, price=10):
        return self.strategy.planning_context_builder.build(DAY, stage="continuous", snapshot={
            A: {"last_price": price, "open": 10, "last_close": 10},
            B: {"last_price": 20, "open": 20, "last_close": 20},
        })

    def current_target(self, quantities=None):
        self.strategy._convergence_suspended = True
        self.strategy.update_target_quantities(quantities or {}, DAY, "existing", version="existing")

    def test_infeasible_keeps_existing_empty_target_and_execution_state(self):
        self.current_target()
        self.strategy._deferred_buys[A] = Decimal(100)
        self.strategy._frozen_instruments[A] = "up_limit"
        with patch.object(self.strategy._target_planner, "plan", side_effect=RuntimeError("infeasible")), \
             patch.object(self.strategy, "_local_safety_plan") as safety:
            result = evaluate_plan(self.strategy, DAY, planning_context=self.context())
        self.assertIsNone(result)
        safety.assert_not_called()
        self.assertEqual(self.strategy._target_version, "existing")
        self.assertEqual(self.strategy._deferred_buys, {A: Decimal(100)})
        self.assertEqual(self.strategy._frozen_instruments, {A: "up_limit"})

    def test_infeasible_without_target_uses_safety_plan(self):
        with patch.object(self.strategy._target_planner, "plan", side_effect=RuntimeError("infeasible")):
            result = evaluate_plan(self.strategy, DAY, planning_context=self.context())
        self.assertFalse(result.persistable)
        self.assertEqual(result.degraded_reason, "risk_manager_failure")

    def test_direction_limits_and_profit_use_one_shared_permission_path(self):
        self.strategy.restore_today_fills(DAY, [{"trade_id": "buy1", "instrument_id": A, "side": "buy", "quantity": 100}])
        self.assertEqual(self.strategy._planning_permissions(A, DAY, planning_context=self.context()), (True, False))
        self.strategy.restore_today_fills(DAY, [{"trade_id": "sell1", "instrument_id": A, "side": "sell", "quantity": 100}])
        request, _ = self.strategy._target_planning_request(DAY, date(2026, 7, 7),
                                                           planning_context=self.context())
        candidate = next(c for c in request.candidates if c.instrument_id == A)
        self.assertFalse(candidate.can_buy)
        self.strategy._active_positions[B] = {"entry_price": 10, "high_price": 10}
        self.assertFalse(self.strategy._planning_permissions(B, DAY, planning_context=self.context())[0])

    def test_partial_stop_sends_locked_remainder_and_sets_final_target(self):
        self.strategy._active_positions[A] = {"entry_price": 10, "high_price": 10,
                                               "entry_date": date(2026, 7, 7)}
        self.strategy.restore_today_fills(DAY, [{"trade_id": "buy1", "instrument_id": A, "side": "buy", "quantity": 400}])
        with patch.object(self.strategy, "_held_instrument_ids", return_value={A}), \
             patch.object(self.strategy, "_current_quantity", return_value=Decimal(1000)):
            plan = evaluate_plan(self.strategy, DAY, planning_context=self.context(9.4))
        request = self.planner.requests[-1]
        holding = next(h for h in request.current_holdings if h.instrument_id == A)
        self.assertEqual(holding.quantity, 400)
        self.assertFalse(holding.can_buy)
        self.assertFalse(holding.can_sell)
        self.assertNotIn(A, {c.instrument_id for c in request.candidates})
        self.assertEqual(next(t.quantity for t in plan.targets if t.instrument_id == A), 400)

    def test_at_limit_is_locked_but_opened_stock_uses_current_state(self):
        self.assertEqual(self.strategy._planning_permissions(A, DAY, planning_context=self.context(11)), (False, False))
        self.assertEqual(self.strategy._planning_permissions(A, DAY, planning_context=self.context(10)), (True, True))

    def test_roundoff_down_limit_candidate_is_locked_in_optimizer_payload(self):
        instrument = self.engine.cache.instrument(InstrumentId.from_str(A))
        values = Equity.to_dict(instrument)
        values["info"]["fields"].update(UpStopPrice=12.88, DownStopPrice=10.54)
        self.engine.cache.add_instrument(Equity.from_dict(values))
        row_key = ("000001.SZ", DAY)
        self.strategy._daily_stock_data[row_key] = replace(
            self.strategy._daily_stock_data[row_key], up_limit=12.88, down_limit=10.54,
        )
        self.assertEqual(self.strategy._price_limits(A), (12.88, 10.54))
        context = self.strategy.planning_context_builder.build(DAY, stage="continuous", snapshot={
            A: {"last_price": 10.540000000000001, "open": 10.54, "last_close": 11.71},
            B: {"last_price": 20, "open": 20, "last_close": 20},
        })
        request, _ = self.strategy._target_planning_request(DAY, date(2026, 7, 7), planning_context=context)
        candidate = next(c for c in request.candidates if c.instrument_id == A)
        payload = RiskManagerModelTargetPlanner._candidate_payload(candidate)
        self.assertEqual(payload["price"], 10.540000000000001)
        self.assertFalse(payload["can_buy"])
        self.assertFalse(payload["can_sell"])

    def test_roundoff_limits_trigger_replanning_and_preserve_execution_direction_rules(self):
        instrument = self.engine.cache.instrument(InstrumentId.from_str(A))
        values = Equity.to_dict(instrument)
        values["info"]["fields"].update(UpStopPrice=12.88, DownStopPrice=10.54)
        self.engine.cache.add_instrument(Equity.from_dict(values))
        row_key = ("000001.SZ", DAY)
        self.strategy._daily_stock_data[row_key] = replace(
            self.strategy._daily_stock_data[row_key], up_limit=12.88, down_limit=10.54,
        )
        for price, reason, side in [(10.540000000000001, "down_limit", OrderSide.SELL),
                                    (math.nextafter(12.88, -math.inf), "up_limit", OrderSide.BUY)]:
            with self.subTest(reason=reason):
                self.strategy.apply_full_tick_snapshot({A: {"last_price": price}}, "test")
                tracker = ReplanTriggerTracker()
                triggers = tracker.observe(DAY, {A: Decimal(200)}, {A: Decimal(100)},
                                           {A: (price, *self.strategy._price_limits(A))})
                self.assertEqual([trigger.reason for trigger in triggers], [reason])
                self.assertEqual(self.strategy._price_limit_reason(A, side), reason)
                other = OrderSide.BUY if side == OrderSide.SELL else OrderSide.SELL
                self.assertIsNone(self.strategy._price_limit_reason(A, other))


    def test_empty_committed_plan_restores_without_calling_planner(self):
        archive = {"quantities": {}, "reason": "empty", "version": "saved-empty", "signal_date": date(2026, 7, 7)}
        archive = committed_archive(archive)
        self.strategy.configure_planning_archive(None, lambda day: archive)
        self.strategy._convergence_suspended = True
        start_runtime(self.strategy)
        self.assertEqual(self.strategy._target_version, "saved-empty")
        self.assertEqual(self.planner.requests, [])


    def test_auction_replay_restores_committed_empty_target_without_replanning(self):
        engine, strategy, planner, _ = replay_fixture(continuous=True)
        archive = {"quantities": {}, "reason": "saved-empty", "version": "saved-empty",
                   "signal_date": date(2026, 7, 7), "target_extra": {}}
        archive = committed_archive(archive)
        strategy.configure_planning_archive(None, lambda day: archive)
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-empty")
            self.assertEqual(strategy._target_quantities, {})
            self.assertEqual(engine.cache.orders(), [])
            self.assertIn(DAY, strategy._processed_dates)
        finally:
            engine.dispose()

    def test_auction_replay_restores_committed_auction_metadata(self):
        engine, strategy, planner, _ = replay_fixture()
        archive = {"quantities": {A: 1000}, "reason": "saved-auction", "version": "saved-auction",
                   "signal_date": date(2026, 7, 7),
                   "target_extra": {A: {"execution_phase": "opening_auction", "prediction_rank": 2}}}
        archive = committed_archive(archive)
        strategy.configure_planning_archive(None, lambda day: archive)
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-auction")
            self.assertIn(DAY, strategy.auction_session.frozen_dates)
            self.assertEqual(strategy.auction_session.ranks[A], 2)
            self.assertEqual(len(engine.cache.orders()), 1)
        finally:
            engine.dispose()

    def test_auction_executes_an_already_processed_target_without_reloading(self):
        engine, strategy, planner, _ = replay_fixture(
            continuous=True,
            transform_config=lambda config: type(config)(**{
                **config.dict(), "auction_start_time": "10:54:01", "auction_submit_cutoff": "10:59:59",
                "auction_execution_time": "10:54:50",
            }),
        )
        archive = {"quantities": {A: 1000}, "reason": "saved-continuous", "version": "saved-continuous",
                   "signal_date": date(2026, 7, 7), "target_extra": {}}
        archive = committed_archive(archive)
        loaded_days = []

        def load_committed(day):
            loaded_days.append(day)
            return archive

        strategy.configure_planning_archive(None, load_committed)
        try:
            engine.run(end=datetime.fromisoformat("2026-07-08T11:00:00+08:00"))
            self.assertEqual(loaded_days, [DAY])
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-continuous")
            self.assertEqual(strategy._target_quantities, {A: Decimal(1000)})
            self.assertIn(DAY, strategy.auction_session.frozen_dates)
            self.assertIn(DAY, auction_contexts(strategy))
            orders = engine.cache.orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(str(orders[0].instrument_id), A)
            self.assertEqual(orders[0].side, OrderSide.BUY)
            self.assertEqual(int(orders[0].quantity), 1000)
            self.assertIn("opening_auction", orders[0].tags)
            self.assertIn(DAY, strategy._processed_dates)
        finally:
            engine.dispose()

    def test_auction_executes_a_restored_continuous_target(self):
        engine, strategy, planner, _ = replay_fixture()
        archive = {"quantities": {A: 1000}, "reason": "saved-continuous", "version": "saved-continuous",
                   "signal_date": date(2026, 7, 7),
                   "target_extra": {A: {"planning_stage": "continuous"}}}
        archive = committed_archive(archive)
        strategy.configure_planning_archive(None, lambda day: archive)
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-continuous")
            self.assertIn(DAY, strategy.auction_session.frozen_dates)
            self.assertEqual(strategy.auction_session.ranks[A], 2)
            orders = engine.cache.orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(str(orders[0].instrument_id), A)
            self.assertEqual(orders[0].side, OrderSide.BUY)
            self.assertEqual(int(orders[0].quantity), 1000)
            self.assertIn("opening_auction", orders[0].tags)
        finally:
            engine.dispose()

    def test_repeated_auction_processing_does_not_reload_or_duplicate_orders(self):
        engine, strategy, planner, _ = replay_fixture()
        receive = strategy.orchestration.scheduled_triggers['auction_planning'].emit
        strategy.orchestration.scheduled_triggers['auction_planning'].emit = lambda occurrence: (
            receive(occurrence), receive(occurrence))
        archive = {"quantities": {A: 1000}, "reason": "saved-continuous", "version": "saved-continuous",
                   "signal_date": date(2026, 7, 7), "target_extra": {}}
        archive = committed_archive(archive)
        loader = MagicMock(return_value=archive)
        strategy.configure_planning_archive(None, loader)
        try:
            engine.run()
            loader.assert_called_once_with(DAY)
            self.assertEqual(planner.requests, [])
            orders = engine.cache.orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(int(orders[0].quantity), 1000)
            self.assertEqual(strategy._auction_buy_committed_notional, Decimal("10050"))
        finally:
            engine.dispose()

    def test_temporary_gate_blocks_all_trading_and_foreign_target_updates(self):
        self.current_target({A: 500})
        self.strategy._replanning_blocked = True
        self.strategy.update_target_quantities({B: 100}, DAY, "foreign", bypass_pause=True)
        self.assertEqual(self.strategy._target_quantities, {A: Decimal(500)})
        with self.assertRaises(RuntimeError):
            self.strategy.submit_order(None)
        with self.assertRaises(RuntimeError):
            self.strategy.submit_order_list(None)
        with self.assertRaises(RuntimeError):
            self.strategy.modify_order(None)
        for operation, args in ((self.strategy.cancel_order, (None,)),
                                (self.strategy.cancel_orders, ([],)),
                                (self.strategy.cancel_all_orders, (None,))):
            with self.assertRaises(RuntimeError):
                operation(*args)


    def test_live_trailing_comparison_uses_corporate_action_adjustment_not_intraday_high(self):
        previous = date(2026, 7, 7)
        self.strategy._daily_stock_data[("000001.SZ", previous)] = DailyStockData(
            stock_code="000001.SZ", trade_date=previous, close=12, adj_factor=2)
        self.strategy.configure_divid_events_loader(lambda codes: {"000001.SZ": [(DAY, .9)]})
        self.strategy._active_positions[A] = {"entry_price": 10, "high_price": 30}
        self.assertAlmostEqual(self.strategy._current_adjustment_factor("000001.SZ", DAY), 2 / .9)
        with patch.object(self.strategy, "_adjusted_entry_cost", return_value=20), \
             patch.object(self.strategy, "_update_trailing_state", wraps=self.strategy._update_trailing_state) as trailing:
            self.strategy._holding_exclusion(DAY, previous, A, 1, previous,
                                              planning_context=self.context(11))
        self.assertAlmostEqual(trailing.call_args.args[1], 11 * 2 / .9)
        self.assertEqual(self.strategy._active_positions[A]["high_price"], 30)

    def test_control_auto_switch_persists_before_changing_memory(self):
        writer = MagicMock()
        server = LiveControlServer(node=SimpleNamespace(), strategy_ref=self.strategy,
                                   account_id="a", trader_id="t", control_writer=writer)
        server._set_auto_replan(False)
        writer.set_auto_replan_enabled.assert_called_once_with("a", "t", False)
        self.assertFalse(self.strategy._auto_replan_enabled)
        writer.set_auto_replan_enabled.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            server._set_auto_replan(True)
        self.assertFalse(self.strategy._auto_replan_enabled)


    def test_auto_trigger_in_backtest_cancels_pending_orders_then_uses_latest_prices(self):
        from backtests.data_providers.historical_ticks import HistoricalTick
        def rows_with_blocked_buy(rows):
            result = [replace(r, ask_price=r.bid_price + .2) if r.kind == "quote" else r
                      for r in rows]
            for clock_time, price in (("14:30:01", 11), ("14:30:04", 11)):
                ts = timestamp(f"2026-07-08 {clock_time}")
                result.append(HistoricalTick("000001.SZ", "quote", ts, ts, price, price,
                                             10000, 10000, 10, 10))
            return result
        engine, strategy, planner, _ = replay_fixture(auction_enabled=False, continuous=True,
                                                      transform_rows=rows_with_blocked_buy)
        records = []
        strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))
        original = planner.plan
        def plan(request):
            if len(planner.requests) == 0:
                return original(request)
            self.assertEqual(strategy.replanning_orders(), [])
            planner.requests.append(request)
            # Framework cleanup crosses the 14:30:02 quote: the limit has opened.
            self.assertEqual(request.open_prices[A], 10)
            candidate = next(c for c in request.candidates if c.instrument_id == A)
            self.assertTrue(candidate.can_buy)
            return ModelTargetPlan(DAY, request.signal_date, [
                TargetInfo("000002.SZ", None, 500, TargetContext(), None, B, False),
            ], "replanned")
        planner.plan = plan
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(strategy.orchestration.planning_state()["replan_counts"]["automatic"], 1)
            self.assertEqual(strategy.orchestration.planning_state()["last_planning"]["status"], "committed")
            self.assertTrue(any(order.is_canceled for order in engine.cache.orders()))
            self.assertEqual(strategy._target_reason, "replanned")
            self.assertEqual(records[-1]["status"], "committed")
        finally:
            engine.dispose()


class ReplanningArchiveTests(unittest.TestCase):
    def test_price_limit_roundoff_does_not_mistake_a_real_opening_for_a_limit(self):
        for price, up, down, expected in [
            (10.540000000000001, 12.88, 10.54, "down_limit"),
            (math.nextafter(12.88, -math.inf), 12.88, 10.54, "up_limit"),
            (10.55, 12.88, 10.54, None),
            (12.87, 12.88, 10.54, None),
            (10.53, 12.88, 10.54, "down_limit"),
            (12.89, 12.88, 10.54, "up_limit"),
            (10.54, None, None, None),
        ]:
            with self.subTest(price=price, up=up, down=down):
                self.assertEqual(price_limit_state(price, up, down), expected)

    def test_snapshot_serialization_preserves_enum_and_date(self):
        context = ModelTargetPlanningContext(DAY, "continuous", {A: ModelTargetInstrumentContext(price=10)})
        encoded = planning_json(context)
        self.assertEqual(encoded["trading_date"], "2026-07-08")
        self.assertEqual(encoded["instruments"][A]["price"], 10)
        json.dumps(encoded)

    def test_committed_empty_plan_deletes_projection_in_same_transaction(self):
        connection = MagicMock()
        writer = LiveSnapshotWriter.for_testing(connection, commit=True)
        payload = PlanningAttempt("r1", DAY, "manual", "old", status="committed").payload()
        payload.update(quantities={}, version="new", signal_date=None, reason="empty")
        writer.write_planning_attempt("account", "trader", DAY, payload)
        queries = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
        self.assertIn("INSERT INTO `live_target_plan`", queries[0])
        self.assertIn("DELETE FROM `live_target_portfolio`", queries[1])
        connection.commit.assert_called_once()

    def test_projection_failure_rolls_back_archive(self):
        connection = MagicMock()
        connection.cursor.return_value.execute.side_effect = [None, RuntimeError("delete failed")]
        writer = LiveSnapshotWriter.for_testing(connection, commit=True)
        payload = PlanningAttempt("r1", DAY, "manual", None, status="committed").payload()
        with self.assertRaises(RuntimeError):
            writer.write_planning_attempt("a", "t", DAY, payload)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_trigger_dedup_and_opening_rearms_each_direction(self):
        tracker = ReplanTriggerTracker()
        quantities = {A: Decimal(100)}
        for target in (Decimal(200), Decimal(0)):
            for price in (11, 9):
                limits = {A: (price, 11, 9)}
                self.assertEqual(len(tracker.observe(DAY, {A: target}, quantities, limits)), 1)
                self.assertEqual(tracker.observe(DAY, {A: target}, quantities, limits), [])
                tracker.observe(DAY, {A: target}, quantities, {A: (10, 11, 9)})
        self.assertEqual(tracker.observe(DAY, {A: Decimal(100)}, quantities, {A: (11, 11, 9)}), [])

    def test_restart_fill_restore_deduplicates_realtime_and_snapshot_trades(self):
        writer = LiveSnapshotWriter.for_testing(MagicMock())
        writer._query = MagicMock(side_effect=[
            [],
            [("t1", A, "buy", 100), ("t2", A, "sell", 200)],
            [("t1", A, "buy", 100), ("t3", B, "sell", 300)],
        ])
        restored = writer.load_strategy_today_fills("a", "t", DAY, "strategy")
        self.assertEqual(restored, [{"trade_id": "t1", "instrument_id": A, "side": "buy", "quantity": 100},
                                    {"trade_id": "t2", "instrument_id": A, "side": "sell", "quantity": 200},
                                    {"trade_id": "t3", "instrument_id": B, "side": "sell", "quantity": 300}])
        self.assertEqual(writer._query.call_args.args[1][-1], "strategy")


    def test_unset_auto_override_preserves_yaml_setting(self):
        writer = LiveControlWriter(engine=SimpleNamespace(), create_tables=False)
        writer._query = MagicMock(return_value=[(None,)])
        self.assertIsNone(writer.load_auto_replan_enabled("a", "t"))
        writer._query.return_value = [(0,)]
        self.assertFalse(writer.load_auto_replan_enabled("a", "t"))


class ReplanningWebControlTests(unittest.TestCase):
    def test_dashboard_replanning_routes_use_existing_pin_and_node_routing(self):
        from fastapi import HTTPException
        from web.app import control_replan, control_auto_replan
        data = SimpleNamespace()
        with patch("web.app._config", return_value=SimpleNamespace(control_pin="1234")), \
             patch("web.app._control_post", return_value={"ok": True, "request_id": "r1"}) as post:
            with self.assertRaises(HTTPException) as invalid:
                control_replan("a", "t", "wrong", data)
            self.assertEqual(invalid.exception.status_code, 403)
            post.assert_not_called()
            self.assertEqual(control_replan("a", "t", "1234", data)["request_id"], "r1")
            post.assert_called_once_with(data, "a", "t", "/control/replan")
            post.reset_mock()
            control_auto_replan("a", "t", False, "1234", data)
            post.assert_called_once_with(data, "a", "t", "/control/auto_replan", {"enabled": False})
