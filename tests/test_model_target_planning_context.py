from __future__ import annotations

from tests.orchestration_helpers import auction_contexts

from concurrent.futures import Future
from dataclasses import replace
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import patch
import unittest

from lives._target_model_node import LiveTargetModelPredictionsStrategy
from lives.broker_data_source import BigQmtBrokerDataSource
from strategies.model_target_planners import ModelTargetPlanningContext
from strategies.model_target_planners import ModelTargetInstrumentContext
from tests.test_opening_auction import replay_fixture, timestamp
from tests.orchestration_helpers import evaluate_plan, advance_clock, flush_requests, committed_archive


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
            context = strategy.planning_context_builder.build(DAY, stage="continuous", snapshot={
                A: {"last_price": 10.6, "open": 10.2, "last_close": 10},
                B: {"last_price": 20, "open": 20, "last_close": 20},
            })
            # Mutating caches after context capture must not affect the request.
            strategy.apply_full_tick_snapshot({A: {"last_price": 12, "open": 12}}, "test")
            plan = evaluate_plan(strategy, DAY, planning_context=context)
            self.assertTrue(plan.persistable)
            self.assertEqual({c.instrument_id: c.open_price for c in planner.requests[0].candidates},
                             {A: 10.6, B: 20})
            target = next(t for t in plan.targets if t.instrument_id == A)
            self.assertEqual(target.target_context.price, 10.6)
            self.assertEqual(target.target_context.planning_stage, "continuous")
        finally:
            engine.dispose()

    def test_auction_execution_refreshes_prices_without_changing_planning_context(self):
        def change_later_quotes(rows):
            return [replace(
                r, ts_event=r.ts_event - (123_000_000 if r.symbol == "000001.SZ" else 457_000_000),
                bid_price=r.bid_price * (1.01 if r.ts_init > timestamp("2026-07-08 09:24:01") else 1),
                ask_price=r.ask_price * (1.01 if r.ts_init > timestamp("2026-07-08 09:24:01") else 1),
            ) if r.kind == "indicative" else r for r in rows]

        engine, strategy, planner, _ = replay_fixture(transform_rows=change_later_quotes)
        try:
            with patch.object(strategy, "_refresh_order_book_depth_subscriptions") as subscribe:
                engine.run()
            subscribe.assert_called_once_with({A: 1000, B: 500})
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual({str(o.instrument_id): float(o.price) for o in engine.cache.orders()},
                             {A: 10.15, B: 20.30})
            context = auction_contexts(strategy)[DAY]
            self.assertEqual(context.ts_init, timestamp("2026-07-08 09:24:01.500"))
            self.assertEqual(context.instruments[A].price, 10)
            self.assertEqual(context.instruments[A].price_ts_event,
                             timestamp("2026-07-08 09:24:00.877"))
            self.assertEqual(strategy.auction_reference(A).price, 10.1)
            self.assertEqual(strategy.auction_reference(A).ts_event,
                             timestamp("2026-07-08 09:24:49.877"))
            self.assertEqual(strategy.auction_reference(B).ts_event,
                             timestamp("2026-07-08 09:24:49.543"))
            self.assertEqual(strategy.auction_reference(A).ts_init,
                             timestamp("2026-07-08 09:24:50.500"))
        finally:
            engine.dispose()

    def test_stop_loss_uses_latest_price_in_context(self):
        engine, strategy, _, _ = replay_fixture(auction_enabled=False)
        try:
            strategy.clock.set_time(timestamp("2026-07-08 10:00:00"))
            strategy._roll_trading_day(DAY)
            strategy._active_positions[A] = {"entry_price": 10, "high_price": 10}
            context = strategy.planning_context_builder.build(DAY, stage="continuous", snapshot={
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
            context = strategy.planning_context_builder.build(DAY, stage="opening_auction", snapshot={
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
            self.assertEqual(subscription_times, [timestamp("2026-07-08 09:24:01.500")])
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
            self.assertNotIn(DAY, strategy.auction_session.frozen_dates)
            self.assertTrue(engine.trader.generate_orders_report().empty)
            self.assertIn(DAY, strategy.auction_session.failed_dates)
        finally:
            engine.dispose()
