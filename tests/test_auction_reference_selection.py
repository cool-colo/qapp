from __future__ import annotations

from decimal import Decimal
from datetime import date
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from nautilus_trader.model.data import BookOrder, OrderBookDepth10
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from lives._target_model_node import LiveTargetModelPredictionsStrategy
from strategies.target_quantities import DepthBookSnapshot
from tests.test_opening_auction import replay_fixture, timestamp
from tests.orchestration_helpers import committed_archive, flush_requests


A = "000001.SZ.QMT"
B = "000002.SZ.QMT"
NOW = timestamp("2026-07-08 09:24:50")


def depth(instrument_id, bid, ask, ts_event, *, ts_init=NOW):
    def levels(price, side):
        first = BookOrder(side, Price.from_str(f"{price:.2f}"), Quantity.from_int(1000), 1)
        empty = BookOrder(OrderSide.NO_ORDER_SIDE, Price.from_int(0), Quantity.from_int(0), 0)
        return ([first] if price > 0 else [empty]) + [empty] * 9

    return OrderBookDepth10(
        InstrumentId.from_str(instrument_id), levels(bid, OrderSide.BUY), levels(ask, OrderSide.SELL),
        [0] * 10, [0] * 10, 0, 0, ts_event, ts_init,
    )


class AuctionReferenceSelectionTest(TestCase):
    def setUp(self):
        self.engine, self.strategy, _, _ = replay_fixture(starting_cash=50000)
        self.strategy.clock.set_time(NOW)
        self.strategy._roll_trading_day(self.strategy._clock_date())

    def tearDown(self):
        self.engine.dispose()

    def update(self, price=10, ts_event=NOW - 2_000_000_000):
        self.strategy.market_snapshots.publish(
            {A: {"last_price": price, "ts_event": ts_event}}, "test_execution",
        )

    def test_newest_exchange_timestamp_wins_and_ties_choose_full_tick(self):
        for full_ts, depth_ts, expected in (
            (NOW - 1_000_000_000, NOW - 2_000_000_000, "whole_market_full_tick"),
            (NOW - 2_000_000_000, NOW - 1_000_000_000, "order_book_depth"),
            (NOW - 1_000_000_000, NOW - 1_000_000_000, "whole_market_full_tick"),
        ):
            with self.subTest(source=expected, full_ts=full_ts):
                self.strategy._depth_books.clear()
                self.update(ts_event=full_ts)
                self.strategy.on_order_book_depth(depth(A, 10.1, 10.3, depth_ts))
                reference, details = self.strategy._select_opening_auction_reference(A)
                self.assertEqual(reference.source, expected)
                self.assertEqual(reference.price, 10 if expected == "whole_market_full_tick" else 10.2)
                self.assertEqual(reference.ts_event, max(full_ts, depth_ts))
                self.assertIn("depth_ts_event", details)
                self.assertIsNone(self.strategy.auction_reference(A))

    def test_depth_midpoint_and_single_sided_replacements(self):
        self.update()
        for index, (bid, ask, expected) in enumerate(((10.1, 10.3, 10.2), (10.4, 0, 10.4), (0, 10.5, 10.5))):
            self.strategy.on_order_book_depth(depth(A, bid, ask, NOW - 1_000_000_000 + index))
            reference, _ = self.strategy._select_opening_auction_reference(A)
            self.assertEqual(reference.source, "order_book_depth")
            self.assertEqual(reference.price, expected)

    def test_invalid_or_missing_candidates_do_not_replace_valid_prices(self):
        for price, ts_event in ((float("nan"), NOW), (float("inf"), NOW), (0, NOW),
                                (10, None), (10, 0), (10, timestamp("2026-07-07 09:24:50"))):
            with self.subTest(price=price, ts_event=ts_event):
                self.update(price, ts_event)
                self.strategy.on_order_book_depth(depth(A, 10.1, 10.3, NOW - 1_000_000_000))
                reference, _ = self.strategy._select_opening_auction_reference(A)
                self.assertEqual(reference.source, "order_book_depth")
        self.strategy._depth_books.clear()
        self.assertIsNone(self.strategy._select_opening_auction_reference(A)[0])
        self.update()
        self.strategy._depth_books[A] = DepthBookSnapshot(
            bids=((float("inf"), 1),), asks=(), ts_event=NOW, ts_init=NOW,
        )
        self.assertEqual(self.strategy._select_opening_auction_reference(A)[0].source,
                         "whole_market_full_tick")
        self.strategy._depth_books[A] = DepthBookSnapshot(
            bids=((10.1, 1),), asks=(), ts_event=timestamp("2026-07-07 09:24:50"), ts_init=NOW,
        )
        self.assertEqual(self.strategy._select_opening_auction_reference(A)[0].source,
                         "whole_market_full_tick")

    def test_out_of_order_depth_cannot_overwrite_a_newer_book(self):
        self.strategy.on_order_book_depth(depth(A, 10.1, 10.3, NOW))
        self.strategy.on_order_book_depth(depth(A, 9.9, 10.1, NOW - 1))
        self.assertEqual(self.strategy._depth_books[A].best_bid, 10.1)
        self.assertEqual(self.strategy._depth_books[A].ts_event, NOW)
        self.strategy.on_order_book_depth(depth(A, 10.2, 0, NOW))
        self.assertIsNone(self.strategy._depth_books[A].best_ask)


class AuctionSubmissionReferenceTest(TestCase):
    def test_native_replay_depth_reaches_strategy_before_auction_submission(self):
        engine, strategy, _, _ = replay_fixture(starting_cash=50000)
        try:
            with patch.object(strategy, "on_order_book_depth", wraps=strategy.on_order_book_depth) as receive, \
                    patch.object(strategy, "_sleep") as sleep:
                engine.run(end=pd.Timestamp("2026-07-08 09:24:59", tz="Asia/Shanghai").to_pydatetime())
            self.assertGreater(receive.call_count, 0)
            sleep.assert_not_called()
            self.assertEqual(strategy._subscribed_order_book_depth_instruments, {A, B})
            contexts = [event.extra["intent_context"] for event in strategy.order_events
                        if event.status == "submitted"]
            self.assertEqual(len(contexts), 2)
            for context in contexts:
                self.assertTrue(context["depth_reference_valid"])
                self.assertEqual(context["depth_ts_event"], NOW)
                self.assertLess(context["whole_market_ts_event"], NOW)
                self.assertEqual(context["price_source"], "order_book_depth")
        finally:
            engine.dispose()

    def test_native_replay_depth_supersedes_an_older_full_market_snapshot(self):
        engine, strategy, _, _ = replay_fixture(starting_cash=50000)
        apply_snapshot = strategy.market_snapshots.publish

        def apply_stale_snapshot(snapshot, trigger, **kwargs):
            snapshot = {instrument_id: dict(fields) for instrument_id, fields in snapshot.items()}
            snapshot[A].update(last_price=9.9, ts_event=NOW - 1_000_000_000)
            return apply_snapshot(snapshot, trigger, **kwargs)

        try:
            with patch.object(strategy.market_snapshots, "publish", side_effect=apply_stale_snapshot):
                engine.run(end=pd.Timestamp("2026-07-08 09:24:59", tz="Asia/Shanghai").to_pydatetime())
            contexts = {event.instrument_id: event.extra["intent_context"]
                        for event in strategy.order_events if event.status == "submitted"}
            self.assertEqual(contexts[A]["price_source"], "order_book_depth")
            self.assertEqual(contexts[A]["reference_price"], 10)
            self.assertEqual(contexts[A]["reference_ts_event"], NOW)
            self.assertEqual(contexts[A]["whole_market_ts_event"], NOW - 1_000_000_000)
            self.assertEqual(contexts[B]["price_source"], "order_book_depth")
            orders = {str(order.instrument_id): order for order in engine.cache.orders()}
            self.assertEqual(float(orders[A].price), 10.05)
            self.assertIn("price_source=order_book_depth",
                          strategy._order_intent_contexts[str(orders[A].client_order_id)].log_text())
        finally:
            engine.dispose()

    def test_live_execution_uses_depth_without_requesting_a_full_tick_update(self):
        from trading_orchestration.components.executors.auction_preparation import AuctionExecutionPreparation
        from task_orchestration.models import TaskContext, Proposal, RunState
        engine, strategy, planner, _ = replay_fixture(strategy_type=lambda config:
            LiveTargetModelPredictionsStrategy(config, refresh_context=None))
        try:
            strategy.clock.set_time(NOW)
            strategy._roll_trading_day(date(2026, 7, 8))
            strategy._target_date, strategy._target_version = date(2026, 7, 8), 'restored'
            strategy._target_quantities = {A: 1000}
            strategy.on_order_book_depth(depth(A, 10.1, 10.3, NOW - 1))
            prepare = AuctionExecutionPreparation(strategy.orchestration)
            context = TaskContext(NOW, 1, 'test', 1, Proposal('test'))
            with patch.object(strategy.market_snapshots, 'request_fresh') as request, \
                    patch.object(strategy, '_schedule_fetch') as fetch:
                self.assertEqual(prepare.prepare(context).state, RunState.SUCCEEDED)
                request.assert_not_called()
                fetch.assert_not_called()
            self.assertNotIn(A, strategy.market_snapshots.latest_snapshot())
            self.assertEqual(strategy._select_opening_auction_reference(A)[0].source, 'order_book_depth')
            self.assertEqual(planner.requests, [])
            self.assertEqual(engine.cache.orders(), [])
        finally:
            engine.dispose()

    def test_new_depth_between_orders_changes_the_next_price_and_audit_source(self):
        engine, strategy, _, _ = replay_fixture(starting_cash=50000)
        real_submit = strategy._submit_order_quantity

        def submit_and_update(*args, **kwargs):
            result = real_submit(*args, **kwargs)
            strategy.on_order_book_depth(depth(A, 10.1, 10.3, NOW + 1))
            return result

        try:
            with patch.object(strategy, "_submit_order_quantity", side_effect=submit_and_update):
                engine.run(end=pd.Timestamp("2026-07-08 09:24:59", tz="Asia/Shanghai").to_pydatetime())
            orders = {str(o.instrument_id): o for o in engine.cache.orders()}
            self.assertEqual(float(orders[B].price), 20.10)
            self.assertEqual(float(orders[A].price), 10.25)
            events = {e.instrument_id: e for e in strategy.order_events if e.status == "submitted"}
            context = events[A].extra["intent_context"]
            self.assertEqual(context["price_source"], "order_book_depth")
            self.assertEqual(context["reference_ts_event"], NOW + 1)
            self.assertLess(context["whole_market_ts_event"], NOW)
            self.assertEqual(strategy.auction_reference(A).source, "order_book_depth")
            self.assertIn("price_source=order_book_depth", strategy._order_intent_contexts[str(orders[A].client_order_id)].log_text())
            expected = sum((o.quantity.as_decimal() * o.price.as_decimal()
                            + strategy._auction_order_splitter.commission(
                                o.quantity.as_decimal() * o.price.as_decimal())
                            for o in orders.values()), Decimal("0"))
            self.assertEqual(strategy._auction_committed_cost, expected)
        finally:
            engine.dispose()

    def test_repriced_slices_respect_the_remaining_cash_budget(self):
        engine, strategy, _, _ = replay_fixture(starting_cash=20200)
        engine.cache.instrument(InstrumentId.from_str(A)).info["limit_order_quantity_rules"]["max_quantity"] = 500
        real_submit = strategy._submit_order_quantity

        def submit_and_update(*args, **kwargs):
            result = real_submit(*args, **kwargs)
            if str(args[1].instrument_id) == A:
                strategy.on_order_book_depth(depth(A, 10.9, 11, NOW + 1))
            return result

        try:
            with patch.object(strategy, "_submit_order_quantity", side_effect=submit_and_update):
                engine.run(end=pd.Timestamp("2026-07-08 09:24:59", tz="Asia/Shanghai").to_pydatetime())
            buys = [o for o in engine.cache.orders() if o.side == OrderSide.BUY]
            a_orders = [o for o in buys if str(o.instrument_id) == A]
            self.assertEqual([float(o.price) for o in a_orders], [10.05, 11.0])
            self.assertEqual([int(o.quantity) for o in a_orders], [500, 400])
            self.assertLessEqual(strategy._auction_committed_cost, Decimal(20200))
            expected_notional = sum((o.quantity.as_decimal() * o.price.as_decimal() for o in buys), Decimal("0"))
            self.assertEqual(strategy._auction_buy_committed_notional, expected_notional)
            # Engine shutdown enters framework cleanup and drops deferred work.
            self.assertEqual(strategy._deferred_buys, {})
            self.assertEqual(strategy._target_quantities[A], Decimal(1000))
        finally:
            engine.dispose()
