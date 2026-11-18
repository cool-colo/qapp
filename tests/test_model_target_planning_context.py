from __future__ import annotations

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


DAY = date(2026, 7, 8)
A = "000001.SZ.QMT"
B = "000002.SZ.QMT"


def accept_execution_update(strategy, trading_date, snapshot, *, ts_init):
    strategy.apply_whole_market_full_tick_snapshot(snapshot, "test_execution")
    return strategy.accept_opening_auction_execution_update(trading_date, ts_init=ts_init)


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
            context = strategy._auction_planning_contexts[DAY]
            self.assertEqual(context.ts_init, timestamp("2026-07-08 09:24:01"))
            self.assertEqual(context.instruments[A].price, 10)
            self.assertEqual(context.instruments[A].price_ts_event,
                             timestamp("2026-07-08 09:24:00.877"))
            self.assertEqual(strategy.auction_reference(A).price, 10.1)
            self.assertEqual(strategy.auction_reference(A).ts_event,
                             timestamp("2026-07-08 09:24:49.877"))
            self.assertEqual(strategy.auction_reference(B).ts_event,
                             timestamp("2026-07-08 09:24:49.543"))
            self.assertEqual(strategy.auction_reference(A).ts_init,
                             timestamp("2026-07-08 09:24:50"))
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
        execution_pending = Future()
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:00"))
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", side_effect=[pending, execution_pending]) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()), \
                    patch.object(strategy, "_refresh_order_book_depth_subscriptions") as subscribe, \
                    patch.object(strategy, "execute_opening_auction") as execute:
                strategy._on_process_targets_timer(None)
                strategy._on_process_targets_timer(None)
                fetch.assert_not_called()
                strategy._schedule_opening_auction()
                self.assertEqual(strategy.clock.next_time_ns(strategy._OPENING_AUCTION_ALERT),
                                 timestamp("2026-07-08 09:24:01"))
                self.assertEqual(strategy.clock.next_time_ns(strategy._OPENING_AUCTION_EXECUTION_ALERT),
                                 timestamp("2026-07-08 09:24:50"))
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
                pending.set_result({
                    A: BigQmtBrokerDataSource._normalize_tick({
                        "time": timestamp("2026-07-08 09:24:00.123") // 1_000_000,
                        "lastPrice": 10, "lastClose": 10,
                    }),
                    B: BigQmtBrokerDataSource._normalize_tick({
                        "time": timestamp("2026-07-08 09:24:00.456") // 1_000_000,
                        "lastPrice": 20, "lastClose": 20,
                    }),
                })
                strategy._on_process_targets_timer(None)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(planner.requests), 1)
                execute.assert_not_called()
                self.assertEqual(engine.cache.orders(), [])
                subscribe.assert_called_once_with({A: 1000, B: 500})
                for handler in strategy.clock.advance_time(timestamp("2026-07-08 09:24:50")):
                    handler.handle()
                self.assertEqual(fetch.call_count, 2)
                execute.assert_not_called()
                self.assertEqual(strategy.clock.next_time_ns(strategy._OPENING_AUCTION_EXECUTION_ALERT),
                                 timestamp("2026-07-09 09:24:50"))
                strategy.clock.set_time(timestamp("2026-07-08 09:24:52"))
                execution_pending.set_result({
                    A: BigQmtBrokerDataSource._normalize_tick({
                        "time": timestamp("2026-07-08 09:24:49.123") // 1_000_000,
                        "lastPrice": 10.1, "lastClose": 10,
                    }),
                    B: BigQmtBrokerDataSource._normalize_tick({
                        "time": timestamp("2026-07-08 09:24:49.987") // 1_000_000,
                        "lastPrice": 20.2, "lastClose": 20,
                    }),
                })
                self.assertEqual(len(planner.requests), 1)
                execute.assert_called_once_with()
                self.assertEqual(strategy._select_opening_auction_reference(A)[0].price, 10.1)
                self.assertEqual(strategy._select_opening_auction_reference(B)[0].price, 20.2)
                self.assertEqual(strategy._auction_planning_contexts[DAY].instruments[A].price_ts_event,
                                 timestamp("2026-07-08 09:24:00.123"))
                for instrument_id, source_time in [(A, "09:24:49.123"), (B, "09:24:49.987")]:
                    source_ns = timestamp(f"2026-07-08 {source_time}")
                    self.assertEqual(strategy._select_opening_auction_reference(instrument_id)[0].ts_event, source_ns)
                    self.assertEqual(strategy._select_opening_auction_reference(instrument_id)[0].ts_init,
                                     timestamp("2026-07-08 09:24:52"))
                strategy._auction_execution_active = True
                intent = strategy._target_order_intent(A, Decimal("1000"))
                self.assertIsNotNone(intent)
                self.assertIn(f"reference_ts_event={timestamp('2026-07-08 09:24:49.123')}",
                              intent.context.log_text())
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
                    phase="planning",
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
            self.assertEqual(sum(c.args == (whole_market_source,) for c in fetch.call_args_list), 2)
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual(len(engine.cache.orders()), 2)
            self.assertEqual({order.ts_init for order in engine.cache.orders()},
                             {timestamp("2026-07-08 09:24:50")})
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

    def test_live_execution_never_reuses_an_inflight_planning_snapshot(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        planning_pending, execution_pending = Future(), Future()
        try:
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", side_effect=[planning_pending, execution_pending]) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()), \
                    patch.object(strategy, "execute_opening_auction") as execute:
                strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
                strategy._on_opening_auction_timer(None)
                strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
                strategy._on_opening_auction_execution_timer(None)
                self.assertEqual(fetch.call_count, 1)
                planning_pending.set_result({A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                             B: {"last_price": 20, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(len(planner.requests), 1)
                self.assertEqual(engine.cache.orders(), [])
                strategy.clock.set_time(timestamp("2026-07-08 09:24:52"))
                execution_pending.set_result({A: {"last_price": 10.1, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                              B: {"last_price": 20.2, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
                execute.assert_called_once_with()
                self.assertEqual(strategy._select_opening_auction_reference(A)[0].price, 10.1)
                self.assertEqual(strategy._select_opening_auction_reference(B)[0].price, 20.2)
                self.assertEqual(strategy._auction_planning_contexts[DAY].instruments[A].price, 10)
                strategy._on_opening_auction_execution_timer(None)
                self.assertEqual(fetch.call_count, 2)
                execute.assert_called_once_with()
        finally:
            engine.dispose()

    def test_live_execution_failure_or_late_completion_keeps_plan_without_orders(self):
        for outcome in ("failed", "empty", "missing", "late"):
            with self.subTest(outcome=outcome):
                engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
                    LiveTargetModelPredictionsStrategy(config, refresh_context=None))
                planning_pending, execution_pending = Future(), Future()
                try:
                    strategy.configure_whole_market_full_tick_source(lambda: {})
                    with patch.object(strategy, "_schedule_fetch", side_effect=[planning_pending, execution_pending]), \
                            patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()):
                        strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
                        strategy._on_opening_auction_timer(None)
                        planning_pending.set_result({A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                                     B: {"last_price": 20, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
                        target_version = strategy._target_version
                        strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
                        strategy._on_opening_auction_execution_timer(None)
                        if outcome == "failed":
                            execution_pending.set_exception(RuntimeError("execution snapshot unavailable"))
                        elif outcome == "empty":
                            execution_pending.set_result({})
                        elif outcome == "missing":
                            execution_pending.set_result({A: {"last_price": 10.1, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10}})
                        else:
                            strategy.clock.set_time(timestamp("2026-07-08 09:24:59"))
                            execution_pending.set_result({A: {"last_price": 10.1, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                                          B: {"last_price": 20.2, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
                    self.assertEqual(len(planner.requests), 1)
                    self.assertEqual(strategy._target_version, target_version)
                    self.assertIn(DAY, strategy._auction_frozen_dates)
                    self.assertIn(DAY, strategy._auction_snapshot_failed_dates)
                    self.assertEqual(engine.cache.orders(), [])
                    self.assertIsNone(strategy.auction_reference(A))
                finally:
                    engine.dispose()

    def test_immediate_execution_fetch_cannot_replace_the_planning_batch(self):
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        planning_pending, execution_ready = Future(), Future()
        execution_ready.set_result({A: {"last_price": 10.1, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                    B: {"last_price": 20.2, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
        try:
            strategy.configure_whole_market_full_tick_source(lambda: {})
            with patch.object(strategy, "_schedule_fetch", side_effect=[planning_pending, execution_ready]) as fetch, \
                    patch.object(strategy, "_run_on_loop", side_effect=lambda callback: callback()), \
                    patch.object(strategy, "execute_opening_auction") as execute:
                strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
                strategy._on_opening_auction_timer(None)
                strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
                strategy._on_opening_auction_execution_timer(None)
                planning_pending.set_result({A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                                             B: {"last_price": 20, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}})
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(len(planner.requests), 1)
                self.assertEqual(strategy._auction_planning_contexts[DAY].instruments[A].price, 10)
                self.assertEqual(strategy._select_opening_auction_reference(A)[0].price, 10.1)
                execute.assert_called_once_with()
        finally:
            engine.dispose()

    def test_execution_requires_plan_and_its_own_snapshot(self):
        engine, strategy, planner, _ = replay_fixture()
        snapshot = {A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 10},
                    B: {"last_price": 20, "ts_event": timestamp("2026-07-08 09:24:49"), "last_close": 20}}
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
            with patch.object(strategy, "execute_opening_auction") as execute:
                strategy.process_opening_auction_execution()
                self.assertEqual(planner.requests, [])
                execute.assert_not_called()
                self.assertTrue(strategy.accept_opening_auction_snapshot(DAY, snapshot,
                                ts_init=timestamp("2026-07-08 09:24:01")))
                strategy.process_opening_auction()
                strategy.process_opening_auction_execution()
                self.assertEqual(len(planner.requests), 1)
                execute.assert_not_called()
                self.assertIsNone(strategy.auction_reference(A))
                self.assertTrue(accept_execution_update(strategy, DAY, snapshot,
                                ts_init=timestamp("2026-07-08 09:24:50")))
                strategy.process_opening_auction_execution()
                execute.assert_called_once_with()
        finally:
            engine.dispose()

    def test_execution_snapshot_requires_only_targets_and_holdings(self):
        engine, strategy, planner, _ = replay_fixture()
        strategy.configure_planning_archive(None, lambda day: {
            "quantities": {A: 1000}, "reason": "saved", "version": "saved",
            "signal_date": date(2026, 7, 7), "target_extra": {},
        })
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:01"))
            strategy.accept_opening_auction_snapshot(DAY, {
                A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49")}, B: {"last_price": 20, "ts_event": timestamp("2026-07-08 09:24:49")},
            }, ts_init=strategy.clock.timestamp_ns())
            strategy.process_opening_auction()
            strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
            self.assertTrue(accept_execution_update(strategy, DAY, {
                A: {"last_price": 10.1, "ts_event": timestamp("2026-07-08 09:24:49")},
            }, ts_init=strategy.clock.timestamp_ns()))
            with patch.object(strategy, "execute_opening_auction") as execute:
                strategy.process_opening_auction_execution()
                execute.assert_called_once_with()
            self.assertEqual(planner.requests, [])
            self.assertEqual(strategy._select_opening_auction_reference(A)[0].price, 10.1)
        finally:
            engine.dispose()

    def test_restarted_execution_restores_targets_before_checking_missing_prices(self):
        engine, strategy, planner, _ = replay_fixture()
        loader = MagicMock(return_value={
            "quantities": {B: 500}, "reason": "saved", "version": "saved",
            "signal_date": date(2026, 7, 7),
            "target_extra": {B: {"execution_phase": "opening_auction", "prediction_rank": 1}},
        })
        strategy.configure_planning_archive(None, loader)
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
            self.assertFalse(accept_execution_update(strategy,
                DAY, {A: {"last_price": 10, "ts_event": timestamp("2026-07-08 09:24:49")}}, ts_init=strategy.clock.timestamp_ns()))
            loader.assert_called_once_with(DAY)
            self.assertEqual(strategy._target_quantities, {B: 500})
            self.assertIn(DAY, strategy._auction_snapshot_failed_dates)
            self.assertNotIn(DAY, strategy._auction_execution_snapshot_dates)
            self.assertEqual(planner.requests, [])
            self.assertEqual(engine.cache.orders(), [])
            self.assertIsNone(strategy.auction_reference(B))
        finally:
            engine.dispose()
