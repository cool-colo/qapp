from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
from dataclasses import FrozenInstanceError
from datetime import date
from unittest.mock import patch
import unittest

from lives._target_model_node import LiveTargetModelPredictionsStrategy
from strategies.model_target_planners import ModelTargetPlanningContext
from strategies.model_target_planners import ModelTargetInstrumentContext
from tests.test_opening_auction import replay_fixture, timestamp


DAY = date(2026, 7, 8)
A = "000001.SZ.QMT"
B = "000002.SZ.QMT"


class ModelTargetPlanningContextTest(unittest.TestCase):
    def test_context_owns_immutable_copies(self):
        instruments = {A: ModelTargetInstrumentContext(price=10.0, open_price=9.9)}
        context = ModelTargetPlanningContext(DAY, "pre_open", instruments)
        instruments[A] = ModelTargetInstrumentContext(price=99)
        self.assertEqual(context.instruments[A].price, 10)
        self.assertEqual(context.instruments[A].open_price, 9.9)
        with self.assertRaises(TypeError):
            context.instruments[A] = ModelTargetInstrumentContext(price=11)
        with self.assertRaises(FrozenInstanceError):
            context.instruments[A].price = 11

    def test_latest_price_sizes_candidates_but_fixed_open_filters_gap(self):
        engine, strategy, planner, _ = replay_fixture(auction_enabled=False)
        try:
            strategy.clock.set_time(timestamp("2026-07-08 10:00:00"))
            strategy._roll_trading_day(DAY)
            context = strategy.build_planning_context(DAY, stage="continuous", snapshot={
                A: {"last_price": 10.6, "open": 10.2, "last_close": 10},
                B: {"last_price": 20, "open": 20, "last_close": 20},
            })
            # Mutating caches after context capture must not affect the request.
            strategy.apply_full_tick_snapshot({A: {"last_price": 12, "open": 12}}, "test")
            plan = strategy.compute_daily_target_plan(DAY, planning_context=context)
            self.assertTrue(plan.persistable)
            self.assertEqual({c.instrument_id: c.open_price for c in planner.requests[0].candidates},
                             {A: 10.6, B: 20})
            target = next(t for t in plan.targets if t.instrument_id == A)
            self.assertEqual(target.target_context.price, 10.6)
            self.assertEqual(target.target_context.planning_stage, "continuous")
        finally:
            engine.dispose()

    def test_auction_snapshot_prices_stay_fixed_after_target_subscription(self):
        def change_later_quotes(rows):
            return [replace(r, bid_price=r.bid_price * 1.01, ask_price=r.ask_price * 1.01)
                    if r.kind == "indicative" and r.ts_init > timestamp("2026-07-08 09:24:01")
                    else r for r in rows]

        engine, strategy, planner, _ = replay_fixture(transform_rows=change_later_quotes)
        try:
            with patch.object(strategy, "_refresh_order_book_depth_subscriptions") as subscribe:
                engine.run()
            subscribe.assert_called_once_with({A: 1000, B: 500})
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual({str(o.instrument_id): float(o.price) for o in engine.cache.orders()},
                             {A: 10.05, B: 20.10})
            context = strategy._auction_planning_contexts[DAY]
            self.assertEqual(context.ts_init, timestamp("2026-07-08 09:24:01"))
            self.assertEqual(context.instruments[A].price, 10)
        finally:
            engine.dispose()

    def test_stop_loss_uses_latest_price_in_context(self):
        engine, strategy, _, _ = replay_fixture(auction_enabled=False)
        try:
            strategy.clock.set_time(timestamp("2026-07-08 10:00:00"))
            strategy._roll_trading_day(DAY)
            strategy._active_positions[A] = {"entry_price": 10, "high_price": 10}
            context = strategy.build_planning_context(DAY, stage="continuous", snapshot={
                A: {"last_price": 9.4, "open": 10, "last_close": 10},
            })
            reason = strategy._holding_exclusion(DAY, date(2026, 7, 7), A, 1,
                                                  planning_context=context)
            self.assertEqual(reason, "stop_triggered")
            self.assertEqual(strategy.signal_events[-1].extra["open_price"], 9.4)
        finally:
            engine.dispose()

    def test_auction_keeps_existing_entry_rules(self):
        engine, strategy, _, _ = replay_fixture()
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
            strategy._roll_trading_day(DAY)
            strategy._st_by_date[DAY] = {"000002.SZ"}
            context = strategy.build_planning_context(DAY, stage="opening_auction", snapshot={
                A: {"last_price": 10.6, "last_close": 10},
                B: {"last_price": 20, "last_close": 20},
            })
            self.assertEqual(strategy._entry_skip_reason("000001.SZ", DAY,
                             planning_context=context), "open_gap_up")
            self.assertEqual(strategy._entry_skip_reason("000002.SZ", DAY,
                             planning_context=context), "st")
        finally:
            engine.dispose()

    def test_auction_targets_subscribe_before_cancel_and_submission_windows(self):
        engine, strategy, _, _ = replay_fixture(continuous=True)
        subscription_times = []
        try:
            with patch.object(strategy, "_refresh_order_book_depth_subscriptions",
                              side_effect=lambda quantities: subscription_times.append(
                                  strategy.clock.timestamp_ns())):
                engine.run()
            self.assertEqual(subscription_times, [timestamp("2026-07-08 09:24:01")])
        finally:
            engine.dispose()

    def test_missing_preparation_price_is_not_repaired_by_later_ticks(self):
        def late_second_stock(rows):
            return [r for r in rows if r.symbol != "000002.SZ"
                    or r.ts_init > timestamp("2026-07-08 09:24:01")]

        engine, strategy, planner, _ = replay_fixture(transform_rows=late_second_stock)
        planning_times = []
        original_plan = planner.plan

        def record_plan(request):
            planning_times.append(strategy.clock.timestamp_ns())
            return original_plan(request)

        planner.plan = record_plan
        try:
            engine.run()
            self.assertTrue(all(ts >= timestamp("2026-07-08 09:25:00") for ts in planning_times))
            self.assertNotIn(DAY, strategy._auction_frozen_dates)
            self.assertTrue(engine.trader.generate_orders_report().empty)
            self.assertIn(DAY, strategy._auction_snapshot_failed_dates)
        finally:
            engine.dispose()

    def test_live_waits_for_one_complete_full_market_request(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        pending = Future()
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:00"))
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", return_value=pending) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()), \
                    patch.object(strategy, "_refresh_order_book_depth_subscriptions") as subscribe, \
                    patch.object(strategy, "execute_opening_auction",
                                 wraps=strategy.execute_opening_auction) as execute:
                strategy._on_process_targets_timer(None)
                strategy._on_process_targets_timer(None)
                fetch.assert_not_called()
                strategy._schedule_opening_auction()
                self.assertEqual(strategy.clock.next_time_ns(strategy._OPENING_AUCTION_ALERT),
                                 timestamp("2026-07-08 09:24:01"))
                for handler in strategy.clock.advance_time(timestamp("2026-07-08 09:24:01")):
                    handler.handle()
                self.assertEqual(strategy.clock.next_time_ns(strategy._OPENING_AUCTION_ALERT),
                                 timestamp("2026-07-09 09:24:01"))
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(planner.requests, [])
                self.assertEqual(engine.cache.orders(), [])
                execute.assert_not_called()
                subscribe.assert_not_called()
                strategy.clock.set_time(timestamp("2026-07-08 09:24:10"))
                pending.set_result({A: {"last_price": 10, "last_close": 10},
                                    B: {"last_price": 20, "last_close": 20}})
                strategy._on_process_targets_timer(None)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(planner.requests), 1)
                execute.assert_called_once_with()
                subscribe.assert_called_once_with({A: 1000, B: 500})
        finally:
            engine.dispose()

    def test_live_snapshot_preparation_does_not_plan_or_execute(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        pending = Future()
        prepared = []
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", return_value=pending), \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()):
                strategy._prepare_opening_auction_snapshot(
                    on_ready=lambda: prepared.append(strategy._auction_planning_contexts[DAY]),
                )
                self.assertEqual(prepared, [])
                pending.set_result({A: {"last_price": 10, "last_close": 10},
                                    B: {"last_price": 20, "last_close": 20}})
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0].instruments[A].price, 10)
            self.assertEqual(planner.requests, [])
            self.assertEqual(engine.cache.orders(), [])
            self.assertEqual(strategy._subscribed_order_book_depth_instruments, set())
        finally:
            engine.dispose()

    def test_live_fetch_failure_is_not_retried(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        pending = Future()
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", return_value=pending) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()):
                strategy._on_opening_auction_timer(None)
                pending.set_exception(RuntimeError("snapshot unavailable"))
                strategy._on_opening_auction_timer(None)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(planner.requests, [])
                self.assertIn(DAY, strategy._auction_snapshot_failed_dates)
        finally:
            engine.dispose()

    def test_live_daily_alert_runs_without_periodic_planning_timer(self):
        engine, strategy, planner, _ = replay_fixture(
            strategy_type=lambda config: LiveTargetModelPredictionsStrategy(config, refresh_context=None),
            transform_config=lambda config: type(config)(**{
                **config.dict(), "process_targets_on_timer": False,
                "whole_market_full_tick_enabled": False,
            }),
        )
        completed = Future()
        completed.set_result({A: {"last_price": 10, "last_close": 10},
                              B: {"last_price": 20, "last_close": 20}})
        whole_market_source = lambda: {}
        try:
            strategy.configure_whole_market_full_tick_source(whole_market_source)
            with patch.object(strategy, "_schedule_fetch", side_effect=lambda source:
                              completed if source is whole_market_source else None) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()):
                engine.run()
            self.assertEqual(sum(c.args == (whole_market_source,) for c in fetch.call_args_list), 1)
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual(len(engine.cache.orders()), 2)
            self.assertEqual({order.ts_init for order in engine.cache.orders()},
                             {timestamp("2026-07-08 09:24:01")})
            self.assertEqual(strategy._subscribed_order_book_depth_instruments, {A, B})
        finally:
            engine.dispose()

    def test_live_late_snapshot_does_not_plan_or_submit(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        pending = Future()
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", return_value=pending), \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()):
                strategy._on_opening_auction_timer(None)
                strategy.clock.set_time(timestamp("2026-07-08 09:24:59"))
                pending.set_result({A: {"last_price": 10, "last_close": 10},
                                    B: {"last_price": 20, "last_close": 20}})
            self.assertEqual(planner.requests, [])
            self.assertEqual(engine.cache.orders(), [])
            self.assertNotIn(DAY, strategy._auction_frozen_dates)
        finally:
            engine.dispose()
