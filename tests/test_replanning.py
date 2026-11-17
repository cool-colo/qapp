from __future__ import annotations

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
        return self.strategy.build_planning_context(DAY, stage="continuous", snapshot={
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
            result = self.strategy.compute_daily_target_plan(DAY, planning_context=self.context())
        self.assertIsNone(result)
        safety.assert_not_called()
        self.assertEqual(self.strategy._target_version, "existing")
        self.assertEqual(self.strategy._deferred_buys, {A: Decimal(100)})
        self.assertEqual(self.strategy._frozen_instruments, {A: "up_limit"})

    def test_infeasible_without_target_uses_safety_plan(self):
        with patch.object(self.strategy._target_planner, "plan", side_effect=RuntimeError("infeasible")):
            result = self.strategy.compute_daily_target_plan(DAY, planning_context=self.context())
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
            plan = self.strategy.compute_daily_target_plan(DAY, planning_context=self.context(9.4))
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
        context = self.strategy.build_planning_context(DAY, stage="continuous", snapshot={
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

    def test_manual_replan_works_with_auto_disabled_and_preserves_user_pause(self):
        async def scenario():
            self.current_target()
            self.strategy.update_target_quantities({}, DAY, SELL_ALL_REASON, version="manual-liquidation")
            self.strategy.replanning.auto_enabled = False
            self.strategy.trading_controller.set_paused(True)
            self.strategy.trading_controller.set_target_updates_paused(True)
            records = []
            self.strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))
            request_id = self.strategy.request_replanning()
            self.assertTrue(self.strategy._replanning_blocked)
            with self.assertRaises(ReplanningBusyError):
                self.strategy.request_replanning()
            with self.assertRaises(ReplanningBusyError):
                self.strategy.trading_controller.sell(None, "manual_sell_all")
            await self.strategy.replanning.task
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(self.strategy.replanning.last["status"], "committed")
            self.assertIn(request_id, self.strategy._target_version)
            self.assertNotEqual(self.strategy._target_reason, SELL_ALL_REASON)
            self.assertTrue(self.strategy._trading_paused)
            self.assertTrue(self.strategy._target_updates_paused)
            self.assertEqual(records[-1]["status"], "committed")
        asyncio.run(scenario())

    def test_replan_infeasible_never_accepts_a_new_target(self):
        async def scenario():
            self.current_target({A: 500})
            self.strategy._pending_replacements[A] = "old replacement"
            with patch.object(self.strategy._target_planner, "plan", side_effect=RuntimeError("infeasible")), \
                 patch.object(self.strategy, "accept_model_target_plan") as accept:
                self.strategy.request_replanning()
                await self.strategy.replanning.task
            accept.assert_not_called()
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.strategy._pending_replacements[A], "old replacement")
            self.assertEqual(self.strategy.replanning.last["status"], "keep_current")
        asyncio.run(scenario())

    def test_archive_failure_does_not_apply_new_target(self):
        async def scenario():
            self.current_target({A: 500})
            def archive(attempt, plan):
                if plan is not None:
                    raise RuntimeError("database unavailable")
            self.strategy.configure_planning_archive(archive)
            self.strategy.request_replanning()
            await self.strategy.replanning.task
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.strategy.replanning.last["status"], "failed")
            self.assertFalse(self.strategy._replanning_blocked)
        asyncio.run(scenario())

    def test_cancel_and_late_fill_settle_before_planner(self):
        async def scenario():
            order = SimpleNamespace(client_order_id="pending", is_pending_cancel=False)
            pending = [order]
            reconciled = []
            self.strategy.replanning_orders = MagicMock(side_effect=lambda: list(pending))
            async def cancel_result():
                await asyncio.sleep(0)
                pending.clear()
                await asyncio.sleep(.02)
                self.strategy._execution_activity += 1
            def cancel(_):
                order.is_pending_cancel = True
                asyncio.create_task(cancel_result())
            self.strategy.cancel_for_replanning = MagicMock(side_effect=cancel)
            async def reconcile():
                reconciled.append(self.strategy._execution_activity)
            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            self.strategy.request_replanning()
            await self.strategy.replanning.task
            self.assertEqual(len(self.planner.requests), 1)
            self.assertTrue(reconciled)
            self.assertEqual(reconciled[-1], 1)
            self.strategy.cancel_for_replanning.assert_called_once()
        asyncio.run(scenario())

    def test_empty_committed_plan_restores_without_calling_planner(self):
        archive = {"quantities": {}, "reason": "empty", "version": "saved-empty", "signal_date": date(2026, 7, 7)}
        self.strategy.configure_planning_archive(None, lambda day: archive)
        self.strategy._convergence_suspended = True
        self.assertTrue(self.strategy._process_trading_day(DAY))
        self.assertEqual(self.strategy._target_version, "saved-empty")
        self.assertEqual(self.planner.requests, [])

    def test_manual_sell_targets_survive_limit_triggers_until_daily_refresh(self):
        held = {A: Decimal(100), B: Decimal(200)}
        with patch.object(self.strategy, "control_open_long_quantities", return_value=held), \
             patch.object(self.strategy, "control_sellable_quantity", side_effect=lambda iid: held[iid]), \
             patch.object(self.strategy, "_held_instrument_ids", return_value=set(held)), \
             patch.object(self.strategy, "_current_quantity", side_effect=lambda iid: held[str(iid)]), \
             patch.object(self.strategy, "_converge_to_target_locked") as execute:
            for instrument_ids, reason in ((None, SELL_ALL_REASON), ([A], SELL_REASON)):
                with self.subTest(reason=reason):
                    self.current_target(held)
                    self.strategy._convergence_suspended = False
                    self.strategy.apply_full_tick_snapshot({
                        A: {"last_price": 11, "open": 10, "last_close": 10},
                        B: {"last_price": 20, "open": 20, "last_close": 20},
                    }, "test")
                    result = self.strategy.trading_controller.sell(instrument_ids, reason)
                    self.assertEqual(result["target_qty"][A], 0)
                    self.assertFalse(self.strategy.replanning.busy)
                    self.assertFalse(self.strategy._replanning_blocked)
                    self.assertEqual(self.strategy._target_quantities[A], Decimal(0))
                    self.assertTrue(execute.called)
                    self.strategy.on_price_snapshot(DAY)
                    self.assertFalse(self.strategy.check_replanning_trigger(DAY))
                    self.assertEqual(self.strategy.replanning.counts["automatic"], 0)

            # The ordinary daily target-loading path takes control again; no user
            # pause or automatic-replanning setting needs to be changed.
            self.strategy.configure_live_target_portfolio_loader(lambda day, signal: [
                {"instrument_id": A, "target_qty": 200, "reason": "daily-model",
                 "target_version": "daily-model"},
            ])
            self.assertTrue(self.strategy._process_trading_day(DAY))
            self.assertEqual(self.strategy._target_reason, "daily-model")
            self.assertTrue(self.strategy.replanning.busy)
            self.assertEqual(self.strategy.replanning.counts["automatic"], 1)

    def _late_limit_scenario(self, *, opens_again):
        async def scenario():
            self.current_target({A: 1000, B: 500})
            loop = asyncio.get_running_loop()
            started, release = asyncio.Event(), threading.Event()
            original = self.planner.plan

            def plan(request):
                if not self.planner.requests:
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise TimeoutError("test did not release optimizer")
                return original(request)

            self.planner.plan = plan
            self.strategy._schedule_convergence.side_effect = self.strategy._converge_to_target
            with patch.object(self.strategy, "_converge_to_target_locked"):
                self.strategy.apply_full_tick_snapshot({
                    A: {"last_price": 11, "open": 10, "last_close": 10},
                    B: {"last_price": 20, "open": 20, "last_close": 20},
                }, "test")
                first_task = self.strategy.replanning.task
                try:
                    await asyncio.wait_for(started.wait(), timeout=3)
                    first_attempt = self.strategy.replanning.current
                    self.strategy.apply_full_tick_snapshot({
                        B: {"last_price": 22, "open": 20, "last_close": 20},
                    }, "test")
                    self.assertIs(self.strategy.replanning.task, first_task)
                    if opens_again:
                        self.strategy.apply_full_tick_snapshot({
                            B: {"last_price": 20, "open": 20, "last_close": 20},
                        }, "test")
                finally:
                    release.set()
                await first_task
                if opens_again:
                    self.assertFalse(self.strategy.replanning.busy)
                    self.assertEqual(len(self.planner.requests), 1)
                    self.assertFalse(self.strategy.check_replanning_trigger(DAY))
                else:
                    self.assertTrue(self.strategy.replanning.busy)
                    second_task = self.strategy.replanning.task
                    self.assertIsNot(second_task, first_task)
                    await second_task
                    self.assertEqual(len(self.planner.requests), 2)
                    self.assertEqual(self.strategy.replanning.counts["automatic"], 2)
                    self.assertFalse(self.strategy.replanning.busy)
                    self.assertEqual(first_attempt.inputs["request"]["open_prices"][B], 20)
                    self.assertNotIn(B, {trigger.instrument_id for trigger in first_attempt.triggers})
                    follow_up = self.strategy.replanning.last
                    self.assertIn(B, {trigger["instrument_id"] for trigger in follow_up["triggers"]})
                    second_request = self.planner.requests[-1]
                    self.assertEqual(second_request.open_prices[B], 22)
                    self.assertFalse(next(c for c in second_request.candidates if c.instrument_id == B).can_buy)
        asyncio.run(scenario())

    def test_limit_after_snapshot_runs_follow_up_planning(self):
        self._late_limit_scenario(opens_again=False)

    def test_late_limit_that_opens_again_does_not_force_another_plan(self):
        self._late_limit_scenario(opens_again=True)

    def test_limit_before_snapshot_is_included_in_current_attempt(self):
        async def scenario():
            self.current_target({A: 1000, B: 500})
            cancel_requested = asyncio.Event()
            order = SimpleNamespace(client_order_id="pending", is_pending_cancel=False)
            pending = [order]
            self.strategy.replanning_orders = MagicMock(side_effect=lambda: list(pending))

            def cancel(_):
                order.is_pending_cancel = True
                cancel_requested.set()

            self.strategy.cancel_for_replanning = MagicMock(side_effect=cancel)
            self.strategy._schedule_convergence.side_effect = self.strategy._converge_to_target
            with patch.object(self.strategy, "_converge_to_target_locked"):
                self.strategy.apply_full_tick_snapshot({
                    A: {"last_price": 11, "open": 10, "last_close": 10},
                }, "test")
                task = self.strategy.replanning.task
                await asyncio.wait_for(cancel_requested.wait(), timeout=3)
                self.strategy.apply_full_tick_snapshot({
                    B: {"last_price": 22, "open": 20, "last_close": 20},
                }, "test")
                pending.clear()
                await task
            self.assertFalse(self.strategy.replanning.busy)
            self.assertEqual(len(self.planner.requests), 1)
            self.assertEqual(self.planner.requests[0].open_prices[B], 22)
            self.assertEqual({trigger["instrument_id"] for trigger in self.strategy.replanning.last["triggers"]},
                             {A, B})
        asyncio.run(scenario())

    def test_stop_before_coroutine_entry_releases_gate_and_allows_restart(self):
        async def scenario():
            records = []
            self.strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))
            self.strategy.request_replanning()
            old_attempt, old_task = self.strategy.replanning.current, self.strategy.replanning.task
            self.strategy.on_stop()
            with self.assertRaises(asyncio.CancelledError):
                await old_task
            await asyncio.sleep(0)
            self.assertFalse(self.strategy.replanning.busy)
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(self.strategy.replanning.last["status"], "interrupted")
            self.assertEqual(len(records), 1)
            self.assertEqual(self.planner.requests, [])
            self.strategy._schedule_convergence.assert_not_called()

            self.strategy.on_start()
            new_id = self.strategy.request_replanning()
            new_task = self.strategy.replanning.task
            # A delayed completion from the canceled request cannot release the
            # next request's gate or overwrite its recorded state.
            self.strategy.replanning._finish(old_attempt, None)
            self.assertTrue(self.strategy._replanning_blocked)
            self.assertEqual(self.strategy.replanning.current.request_id, new_id)
            await asyncio.wrap_future(new_task)
            self.assertFalse(self.strategy.replanning.busy)
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(self.strategy.replanning.last["status"], "committed")
        asyncio.run(scenario())

    def test_stop_before_entry_on_configured_node_loop_releases_gate(self):
        async def scenario():
            self.strategy._async_scheduler.set_loop(asyncio.get_running_loop())
            records = []
            self.strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", RuntimeWarning)
                self.strategy.request_replanning()
                task = self.strategy.replanning.task
                self.strategy.on_stop()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wrap_future(task)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(self.strategy._replanning_blocked)
                self.assertFalse(self.strategy.replanning.busy)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "interrupted")
                self.assertEqual(self.planner.requests, [])
                self.assertEqual([str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)], [])
        asyncio.run(scenario())

    def test_stop_after_coroutine_entry_archives_interruption_once(self):
        async def scenario():
            records, entered = [], asyncio.Event()
            self.strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))

            async def reconcile():
                entered.set()
                await asyncio.Event().wait()

            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            self.strategy.request_replanning()
            task = self.strategy.replanning.task
            await asyncio.wait_for(entered.wait(), timeout=3)
            self.strategy.on_stop()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertFalse(self.strategy.replanning.busy)
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(sum(row["status"] == "interrupted" for row in records), 1)
            self.strategy._schedule_convergence.assert_not_called()
        asyncio.run(scenario())

    def test_cross_thread_cancellation_keeps_gate_until_started_coroutine_stops(self):
        async def scenario():
            self.strategy._async_scheduler.set_loop(asyncio.get_running_loop())
            records, entered = [], asyncio.Event()
            self.strategy.configure_planning_archive(lambda attempt, plan: records.append(attempt.payload(plan)))

            async def reconcile():
                entered.set()
                await asyncio.Event().wait()

            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            self.strategy.request_replanning()
            task = self.strategy.replanning.task
            await asyncio.wait_for(entered.wait(), timeout=3)
            self.strategy.on_stop()
            self.assertTrue(task.cancelled())
            self.assertTrue(self.strategy._replanning_blocked)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wrap_future(task)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(self.strategy.replanning.busy)
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(sum(row["status"] == "interrupted" for row in records), 1)
            self.strategy._schedule_convergence.assert_not_called()
        asyncio.run(scenario())

    def test_auction_replay_restores_committed_empty_target_without_replanning(self):
        engine, strategy, planner, _ = replay_fixture(continuous=True)
        archive = {"quantities": {}, "reason": "saved-empty", "version": "saved-empty",
                   "signal_date": date(2026, 7, 7), "target_extra": {}}
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
        strategy.configure_planning_archive(None, lambda day: archive)
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-auction")
            self.assertIn(DAY, strategy._auction_frozen_dates)
            self.assertEqual(strategy._auction_prediction_ranks[A], 2)
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
            self.assertIn(DAY, strategy._auction_frozen_dates)
            self.assertIn(DAY, strategy._auction_planning_contexts)
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
        strategy.configure_planning_archive(None, lambda day: archive)
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._target_version, "saved-continuous")
            self.assertIn(DAY, strategy._auction_frozen_dates)
            self.assertEqual(strategy._auction_prediction_ranks[A], 2)
            orders = engine.cache.orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(str(orders[0].instrument_id), A)
            self.assertEqual(orders[0].side, OrderSide.BUY)
            self.assertEqual(int(orders[0].quantity), 1000)
            self.assertIn("opening_auction", orders[0].tags)
        finally:
            engine.dispose()

    def test_repeated_auction_processing_does_not_reload_or_duplicate_orders(self):
        class RepeatedAuctionStrategy(BacktestTargetModelPredictionsStrategy):
            def process_opening_auction(self):
                super().process_opening_auction()
                super().process_opening_auction()

        engine, strategy, planner, _ = replay_fixture(strategy_type=RepeatedAuctionStrategy)
        archive = {"quantities": {A: 1000}, "reason": "saved-continuous", "version": "saved-continuous",
                   "signal_date": date(2026, 7, 7), "target_extra": {}}
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
        self.strategy.cancel_order(None)
        self.strategy.cancel_orders([])
        self.strategy.cancel_all_orders(None)

    def test_cache_query_error_aborts_before_planning(self):
        async def scenario():
            self.current_target({A: 500})
            with patch.object(self.strategy, "replanning_orders", side_effect=RuntimeError("cache unavailable")):
                self.strategy.request_replanning()
                await self.strategy.replanning.task
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.strategy.replanning.last["status"], "failed")
        asyncio.run(scenario())

    def test_initial_planning_also_excludes_concurrent_planning(self):
        self.strategy.replanning.initial_in_progress = True
        with self.assertRaises(ReplanningBusyError):
            self.strategy.request_replanning()
        with self.assertRaises(ReplanningBusyError):
            self.strategy.compute_daily_target_plan(DAY)

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
        self.assertFalse(self.strategy.replanning.auto_enabled)
        writer.set_auto_replan_enabled.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            server._set_auto_replan(True)
        self.assertFalse(self.strategy.replanning.auto_enabled)

    def test_superseded_initial_result_cannot_replace_a_committed_target(self):
        plan = self.strategy.compute_daily_target_plan(DAY, planning_context=self.context())
        self.current_target({A: 500})
        writer = MagicMock()
        recorder = SnapshotRecorder(SnapshotRecorderConfig(account_id="a", trader_id="t"),
                                    writer, self.strategy)
        self.engine.add_actor(recorder)
        self.assertFalse(recorder.persist_strategy_target_plan(plan))
        writer.write_planning_attempt.assert_not_called()
        self.assertEqual(self.strategy._target_version, "existing")

    def test_cancellation_timeout_does_not_call_optimizer(self):
        async def scenario():
            self.current_target({A: 500})
            self.strategy._execution_state_reconciler._timeout_secs = .05
            order = SimpleNamespace(client_order_id="pending", is_pending_cancel=False)
            self.strategy.replanning_orders = MagicMock(return_value=[order])
            self.strategy.cancel_for_replanning = MagicMock()
            self.strategy.request_replanning()
            await self.strategy.replanning.task
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertIn("terminal orders", self.strategy.replanning.last["error"])
        asyncio.run(scenario())

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
            self.assertEqual(request.open_prices[A], 11)
            candidate = next(c for c in request.candidates if c.instrument_id == A)
            self.assertFalse(candidate.can_buy)
            return ModelTargetPlan(DAY, request.signal_date, [
                TargetInfo("000002.SZ", None, 500, TargetContext(), None, B, False),
            ], "replanned")
        planner.plan = plan
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(strategy.replanning.counts["automatic"], 1)
            self.assertEqual(strategy.replanning.last["status"], "committed")
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

    def test_restart_marks_unfinished_attempts_interrupted_and_restores_counts(self):
        writer = LiveSnapshotWriter.for_testing(MagicMock())
        writer._execute = MagicMock()
        writer._query = MagicMock(return_value=[("initial", 1), ("automatic", 3), ("manual", 2)])
        self.assertEqual(writer.restore_planning_state("a", "t", DAY), {"automatic": 3, "manual": 2})
        self.assertIn("interrupted", writer._execute.call_args.args[0])

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
