from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tests.test_opening_auction import replay_fixture, timestamp
from strategies.model_target_planners import ModelTargetPlan, TargetInfo, TargetContext
from strategies.replan_triggers import normalize_scheduled_replan_times
from strategies.strategy_params import StrategyParams
from strategies.trading_control import SELL_ALL_REASON


DAY = date(2026, 7, 8)
A, B = "000001.SZ.QMT", "000002.SZ.QMT"


def with_schedule(config, times):
    return type(config)(**{**config.dict(), "scheduled_replan_times": times})


class ScheduledReplanConfigTests(unittest.TestCase):
    def test_config_normalizes_sorts_and_deduplicates(self):
        params = StrategyParams(scheduled_replan_times=["14:00", "10:00:00", "10:00"])
        self.assertEqual(params.scheduled_replan_times, ("10:00:00", "14:00:00"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.yaml"
            path.write_text('scheduled_replan_times: ["14:00", "10:00"]\n')
            self.assertEqual(StrategyParams.from_yaml(str(path)).scheduled_replan_times,
                             ("10:00:00", "14:00:00"))

    def test_invalid_times_fail_at_config_loading_and_strategy_construction(self):
        for values in (None, "10:00", [1000], ["9:30"], ["25:00"], ["10:61"],
                       ["10:00:60"], ["09:20"], ["11:30"], ["12:00"], ["15:00"]):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "scheduled_replan_times"):
                    StrategyParams(scheduled_replan_times=values)
        engine, strategy, _, _ = replay_fixture(auction_enabled=False)
        try:
            with self.assertRaisesRegex(ValueError, "scheduled_replan_times"):
                type(strategy)(with_schedule(strategy.config, ("12:00",)))
        finally:
            engine.dispose()

    def test_empty_default_and_session_boundaries(self):
        self.assertEqual(StrategyParams().scheduled_replan_times, ())
        self.assertEqual(normalize_scheduled_replan_times(["09:30", "11:29:59", "13:00", "14:59:59"]),
                         ("09:30:00", "11:29:59", "13:00:00", "14:59:59"))


class ScheduledReplanningTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, self.planner, _ = replay_fixture(
            auction_enabled=False, transform_config=lambda c: with_schedule(
                c, ("10:00", "10:00:01", "10:00:02", "14:00")))
        self.strategy.clock.set_time(timestamp("2026-07-08 09:59:59"))
        self.strategy._roll_trading_day(DAY)
        self.snapshot(10)
        self.strategy._convergence_suspended = True
        self.strategy.update_target_quantities({}, DAY, "existing", version="existing")
        self.scheduler = self.strategy._scheduled_replan_triggers
        self.scheduler.start()

    def tearDown(self):
        self.engine.dispose()

    def snapshot(self, price):
        self.strategy.apply_full_tick_snapshot({
            A: {"last_price": price, "open": 10, "last_close": 10},
            B: {"last_price": 20, "open": 20, "last_close": 20},
        }, "test")

    def fire(self, clock_time="10:00:00"):
        self.strategy.clock.set_time(timestamp(f"2026-07-08 {clock_time}"))
        self.scheduler._on_alert(clock_time)

    def test_outside_execution_window_replans_empty_target_without_orders(self):
        async def scenario():
            records = []
            self.strategy.configure_planning_archive(lambda a, p: records.append(a.payload(p)))
            self.assertFalse(self.strategy._within_execution_window())
            self.assertFalse(self.strategy._within_cancel_window())
            self.fire()
            await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.last["status"], "committed")
            self.assertEqual(self.strategy.replanning.counts["automatic"], 1)
            self.assertNotEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.engine.cache.orders(), [])
            self.assertEqual(records[-1]["source"], "automatic")
            self.assertEqual(records[-1]["triggers"][0]["kind"], "scheduled")
            self.assertEqual(records[-1]["validity"]["expires_at"], "2026-07-08T11:30:00+08:00")
        asyncio.run(scenario())

    def test_limit_and_manual_triggers_keep_original_window_requirements(self):
        self.snapshot(11)
        self.strategy.update_target_quantities({A: 100}, DAY, "existing", version="buy")
        self.assertFalse(self.strategy.check_replanning_trigger(DAY))
        with self.assertRaisesRegex(ValueError, "execution and cancellation window"):
            self.strategy.request_replanning()
        self.assertEqual(self.planner.requests, [])

    def test_automatic_guards_skip_scheduled_event(self):
        for flag in ("auto", "trading_pause", "target_pause", "manual_target", "no_target",
                     "previous_target", "non_trading_day"):
            with self.subTest(flag=flag):
                self.strategy.replanning.auto_enabled = flag != "auto"
                self.strategy._trading_paused = flag == "trading_pause"
                self.strategy._target_updates_paused = flag == "target_pause"
                self.strategy._target_reason = SELL_ALL_REASON if flag == "manual_target" else "existing"
                self.strategy._target_version = "" if flag == "no_target" else "existing"
                self.strategy._target_date = date(2026, 7, 7) if flag == "previous_target" else DAY
                self.strategy._trading_dates = [] if flag == "non_trading_day" else [DAY]
                self.strategy.clock.set_time(timestamp("2026-07-08 09:59:59"))
                self.scheduler.start()
                self.fire()
                self.assertFalse(self.strategy.replanning.busy)
                self.assertEqual(self.scheduler.pending, [])
                self.assertEqual(self.planner.requests, [])

    def test_busy_events_merge_and_followup_uses_fresh_inputs(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def reconcile():
                if not entered.is_set():
                    entered.set()
                    await release.wait()
            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            records = []
            def archive(attempt, plan):
                records.append(attempt.payload(plan))
                if attempt.status == "committed" and len(self.planner.requests) == 1:
                    self.snapshot(10.5)
            self.strategy.configure_planning_archive(archive)
            self.fire()
            first = self.strategy.replanning.task
            await entered.wait()
            self.fire("10:00:01")
            self.fire("10:00:02")
            self.assertIs(self.strategy.replanning.task, first)
            self.assertEqual(len(self.scheduler.pending), 2)
            self.assertEqual(len(self.strategy.replanning.current.triggers), 1)
            release.set()
            await first
            await self.strategy.replanning.task
            committed = [record for record in records if record["status"] == "committed"]
            self.assertEqual(len(committed), 2)
            self.assertEqual(len(committed[1]["triggers"]), 2)
            self.assertEqual([r.open_prices[A] for r in self.planner.requests], [10, 10.5])
            self.assertEqual(self.strategy.replanning.counts["automatic"], 2)
            self.assertEqual(self.scheduler.pending, [])
            self.assertEqual(self.engine.cache.orders(), [])
        asyncio.run(scenario())

    def test_pending_event_rechecks_pause_and_expiry(self):
        for clock_time, paused in (("10:01:00", True), ("11:30:00", False), ("13:00:00", False)):
            with self.subTest(clock_time=clock_time):
                self.strategy.clock.set_time(timestamp("2026-07-08 09:59:59"))
                self.strategy._trading_paused = False
                self.scheduler.start()
                self.strategy.replanning.initial_in_progress = True
                self.fire()
                self.assertEqual(len(self.scheduler.pending), 1)
                self.strategy.replanning.initial_in_progress = False
                self.strategy._trading_paused = paused
                self.strategy.clock.set_time(timestamp(f"2026-07-08 {clock_time}"))
                self.scheduler.drain()
                self.assertEqual(self.scheduler.pending, [])
                self.assertFalse(self.strategy.replanning.busy)

    def test_initial_planning_completion_wakes_pending_after_return(self):
        async def scenario():
            def plan(request):
                self.fire()
                return ModelTargetPlan(DAY, request.signal_date, [], "initial")
            with patch.object(self.strategy._target_planner, "plan", side_effect=plan):
                result = self.strategy.compute_daily_target_plan(DAY)
            self.assertEqual(len(self.scheduler.pending), 1)
            self.assertFalse(self.strategy.replanning.busy)
            self.assertIn(self.scheduler._PENDING_ALERT, self.strategy.clock.timer_names)
            self.strategy.accept_model_target_plan(result)
            for handler in self.strategy.clock.advance_time(timestamp("2026-07-08 10:00:00.002")):
                handler.handle()
            await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.last["status"], "committed")
            self.assertEqual(self.scheduler.pending, [])
        asyncio.run(scenario())

    def test_initial_planning_reservation_race_retains_scheduled_request(self):
        self.strategy.replanning.initial_in_progress = True
        self.fire()
        self.strategy.replanning.initial_in_progress = False
        submit = self.strategy.submit_automatic_replanning
        def reserve(triggers, validity):
            self.strategy.replanning.initial_in_progress = True
            return submit(triggers, validity)
        with patch.object(self.strategy, "submit_automatic_replanning", side_effect=reserve):
            self.scheduler.drain()
        self.assertEqual(len(self.scheduler.pending), 1)
        self.assertIsNone(self.strategy.replanning.current)
        self.strategy.replanning.initial_in_progress = False
        self.scheduler.wake_pending()
        self.assertIn(self.scheduler._PENDING_ALERT, self.strategy.clock.timer_names)

    def test_scheduled_busy_request_does_not_join_manual_attempt(self):
        async def scenario():
            self.strategy._within_execution_window = MagicMock(return_value=True)
            self.strategy._within_cancel_window = MagicMock(return_value=True)
            self.strategy._schedule_convergence = MagicMock()
            self.strategy.request_replanning()
            first = self.strategy.replanning.task
            self.fire()
            event = self.scheduler.pending[0]
            self.assertIsNone(self.strategy.submit_automatic_replanning([event], event.validity))
            self.assertEqual(self.strategy.replanning.current.triggers, [])
            self.assertEqual(self.strategy.replanning.current.follow_up_triggers, [])
            await first
            await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.counts, {"automatic": 1, "manual": 1})
        asyncio.run(scenario())

    def test_active_orders_without_cancel_window_abort_before_optimizer(self):
        async def scenario():
            order = SimpleNamespace(client_order_id="pending", is_pending_cancel=False)
            with patch.object(self.strategy, "replanning_orders", return_value=[order]), \
                 patch.object(self.strategy, "cancel_for_replanning") as cancel:
                self.fire()
                await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.last["status"], "failed")
            self.assertIn("cancellation window", self.strategy.replanning.last["error"])
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertFalse(self.strategy._replanning_blocked)
            cancel.assert_not_called()
        asyncio.run(scenario())

    def test_active_orders_are_canceled_and_late_fill_settles_before_capture(self):
        async def scenario():
            pending = [SimpleNamespace(client_order_id="pending", is_pending_cancel=False)]
            self.strategy._within_cancel_window = MagicMock(return_value=True)
            self.strategy.replanning_orders = MagicMock(side_effect=lambda: list(pending))
            def cancel(order):
                pending.clear()
                self.strategy._execution_activity += 1
                self.snapshot(10.25)
            self.strategy.cancel_for_replanning = MagicMock(side_effect=cancel)
            self.fire()
            await self.strategy.replanning.task
            self.strategy.cancel_for_replanning.assert_called_once()
            self.assertEqual(self.strategy.replanning.last["status"], "committed")
            self.assertEqual(self.planner.requests[0].open_prices[A], 10.25)
        asyncio.run(scenario())

    def test_optimizer_failure_preserves_execution_state(self):
        async def scenario():
            self.strategy._deferred_buys[A] = Decimal(100)
            self.strategy._frozen_instruments[A] = "up_limit"
            with patch.object(self.strategy._target_planner, "plan", side_effect=RuntimeError("infeasible")):
                self.fire()
                await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.last["status"], "keep_current")
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.strategy._deferred_buys, {A: Decimal(100)})
            self.assertEqual(self.strategy._frozen_instruments, {A: "up_limit"})
        asyncio.run(scenario())

    def test_reconciliation_failure_preserves_target(self):
        async def scenario():
            async def reconcile():
                raise RuntimeError("reconciliation failed")
            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            self.fire()
            await self.strategy.replanning.task
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy._target_version, "existing")
            self.assertEqual(self.strategy.replanning.last["status"], "failed")
        asyncio.run(scenario())

    def test_changed_execution_or_expired_result_never_replaces_target(self):
        for change in ("fill", "order", "session_end", "next_day"):
            with self.subTest(change=change):
                async def scenario():
                    self.strategy.clock.set_time(timestamp("2026-07-08 09:59:59"))
                    self.scheduler.start()
                    def finish(*args, **kwargs):
                        if change == "fill":
                            self.strategy._execution_activity += 1
                        elif change == "order":
                            self.strategy.replanning_orders = MagicMock(return_value=["pending"])
                        elif change == "session_end":
                            self.strategy.clock.set_time(timestamp("2026-07-08 11:30:00"))
                        else:
                            self.strategy.clock.set_time(timestamp("2026-07-09 10:00:00"))
                        return ModelTargetPlan(DAY, date(2026, 7, 7), [], "replacement")
                    with patch.object(self.strategy, "finish_target_planning", side_effect=finish):
                        self.fire()
                        await self.strategy.replanning.task
                    self.assertEqual(self.strategy.replanning.last["status"], "failed")
                    self.assertEqual(self.strategy._target_version, "existing")
                    self.assertFalse(self.strategy._replanning_blocked)
                    if change == "order":
                        del self.strategy.replanning_orders
                asyncio.run(scenario())

    def test_archival_failure_never_accepts_target(self):
        async def scenario():
            def archive(attempt, plan):
                if plan is not None:
                    raise RuntimeError("archive failed")
            self.strategy.configure_planning_archive(archive)
            self.fire()
            await self.strategy.replanning.task
            self.assertEqual(self.strategy.replanning.last["status"], "failed")
            self.assertEqual(self.strategy._target_version, "existing")
        asyncio.run(scenario())

    def test_stop_cancels_busy_attempt_and_discards_followup(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def reconcile():
                entered.set()
                await release.wait()
            self.strategy._execution_state_reconciler.reconcile_once = reconcile
            self.fire()
            task = self.strategy.replanning.task
            await entered.wait()
            self.fire("10:00:01")
            self.strategy.on_stop()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.scheduler.pending, [])
            self.assertFalse(self.strategy._replanning_blocked)
            self.assertEqual(self.planner.requests, [])
        asyncio.run(scenario())

    def test_restart_schedules_future_times_and_stop_clears_queue(self):
        self.strategy.replanning.initial_in_progress = True
        self.fire()
        self.assertEqual(len(self.strategy.replanning.state()["scheduled_replan_pending"]), 1)
        self.scheduler.stop()
        self.strategy.clock.set_time(timestamp("2026-07-08 10:30:00"))
        self.scheduler.start()
        next_times = self.scheduler.state()["scheduled_replan_next_times"]
        self.assertEqual(next_times["10:00:00"], "2026-07-09T10:00:00+08:00")
        self.assertEqual(next_times["14:00:00"], "2026-07-08T14:00:00+08:00")
        self.assertEqual(self.scheduler.pending, [])
        self.strategy.on_stop()
        self.assertFalse(any(name.startswith(self.scheduler._ALERT_PREFIX)
                             for name in self.strategy.clock.timer_names))
        self.assertEqual(self.scheduler.state()["scheduled_replan_next_times"], {})


class ScheduledReplanningReplayTests(unittest.TestCase):
    def test_testclock_replans_at_ten_and_executes_new_target_only_in_order_window(self):
        from backtests.data_providers.historical_ticks import HistoricalTick
        def rows_with_morning_prices(rows):
            for clock_time in ("09:59:59", "10:00:01", "14:30:01", "14:30:04"):
                for code, price in (("000001.SZ", 10), ("000002.SZ", 20)):
                    ts = timestamp(f"2026-07-08 {clock_time}")
                    rows.append(HistoricalTick(code, "quote", ts, ts, price, price + .01,
                                               10000, 10000, price, price))
            return rows
        engine, strategy, planner, _ = replay_fixture(
            auction_enabled=False, continuous=True, transform_rows=rows_with_morning_prices,
            transform_config=lambda c: with_schedule(c, ("10:00",)))
        original = planner.plan
        committed = []
        strategy.configure_planning_archive(
            lambda attempt, plan: committed.append((strategy.clock.timestamp_ns(), attempt.payload(plan)))
            if attempt.status == "committed" else None)
        def plan(request):
            if not planner.requests:
                return original(request)
            planner.requests.append(request)
            self.assertEqual(engine.cache.orders(), [])
            return ModelTargetPlan(DAY, request.signal_date, [
                TargetInfo("000002.SZ", None, 500, TargetContext(), None, B, False),
            ], "scheduled_rotation")
        planner.plan = plan
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(len(committed), 1)
            self.assertLess(committed[0][0], timestamp("2026-07-08 10:00:01"))
            self.assertEqual(strategy._target_quantities, {B: Decimal(500)})
            self.assertTrue(engine.cache.orders())
            for order in engine.cache.orders():
                self.assertEqual(str(order.instrument_id), B)
                self.assertGreaterEqual(order.ts_init, timestamp("2026-07-08 14:30:00"))
        finally:
            engine.dispose()
