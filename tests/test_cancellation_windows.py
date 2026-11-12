from __future__ import annotations

import unittest
from datetime import time

from strategies.cancellation import cancel_window_index
from strategies.strategy_params import StrategyParams
from tests.test_opening_auction import replay_fixture, timestamp


class CancellationWindowsTest(unittest.TestCase):
    def test_second_precision_and_exclusive_end(self):
        windows = "14:56:59-14:57:00"
        self.assertIsNone(cancel_window_index(time(14, 56, 58), windows))
        self.assertEqual(cancel_window_index(time(14, 56, 59, 999999), windows), 0)
        self.assertIsNone(cancel_window_index(time(14, 57), windows))
        self.assertIsNone(cancel_window_index(time(14, 56, 59), ""))
        for key in ("cancel_windows", "exchange_cancel_windows"):
            for value in ("13:00-15:00", "14:57-14:56", "13:00-14:00,13:30-14:57", "invalid"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    StrategyParams(**{key: value})

    def test_all_cancel_apis_obey_window_without_auction_enabled(self):
        for clock_time, allowed in [("14:56:59", True), ("14:57:00", False)]:
            for method in ("cancel_order", "cancel_orders", "cancel_all_orders"):
                with self.subTest(time=clock_time, api=method):
                    engine, strategy, _, _ = replay_fixture(auction_enabled=False, continuous=True)
                    try:
                        engine.run()
                        order = engine.cache.orders_open()[0]
                        commands = []
                        execute = engine.kernel.exec_engine.execute
                        strategy.msgbus.deregister("ExecEngine.execute", execute)
                        strategy.msgbus.register("ExecEngine.execute", commands.append)
                        ts = timestamp(f"2026-07-08 {clock_time}")
                        strategy.clock.set_time(ts)
                        strategy._last_market_data_ts_event = ts
                        if method == "cancel_order":
                            strategy.cancel_order(order)
                        elif method == "cancel_orders":
                            strategy.cancel_orders([order])
                        else:
                            strategy.cancel_all_orders(order.instrument_id)
                        self.assertEqual(len(commands), int(allowed))
                    finally:
                        strategy.msgbus.deregister("ExecEngine.execute", commands.append)
                        strategy.msgbus.register("ExecEngine.execute", execute)
                        engine.dispose()

    def test_delayed_market_data_does_not_unlock_cancel_after_cutoff(self):
        engine, strategy, _, _ = replay_fixture()
        try:
            engine.run()
            strategy.clock.set_time(timestamp("2026-07-08 14:57:00"))
            strategy._last_market_data_ts_event = timestamp("2026-07-08 14:56:59")
            self.assertFalse(strategy._within_cancel_window())
            strategy.clock.set_time(timestamp("2026-07-08 13:00:00"))
            strategy._last_market_data_ts_event = timestamp("2026-07-08 11:29:59")
            self.assertFalse(strategy._within_cancel_window())
            strategy._last_market_data_ts_event = timestamp("2026-07-07 13:00:00")
            self.assertFalse(strategy._within_cancel_window())
            strategy._last_market_data_ts_event = timestamp("2026-07-08 13:00:01")
            self.assertFalse(strategy._within_cancel_window())
        finally:
            engine.dispose()

    def test_automatic_cancel_after_stop_time_does_not_submit_replacements(self):
        engine, strategy, _, _ = replay_fixture(closing_cancel_time="14:56:59")
        try:
            engine.run()
            orders = engine.cache.orders()
            self.assertEqual(len(orders), 2)
            self.assertTrue(all(order.is_canceled for order in orders))
            self.assertTrue(all(order.ts_last == timestamp("2026-07-08 14:56:59") for order in orders))
            self.assertTrue(strategy._stop_time_reached())
            self.assertFalse(strategy._within_execution_window())
        finally:
            engine.dispose()

    def test_automatic_cancel_is_blocked_at_closing_auction_start(self):
        engine, strategy, _, _ = replay_fixture(closing_cancel_time="14:57:00")
        try:
            engine.run()
            self.assertEqual(len(engine.cache.orders()), 2)
            self.assertFalse(any(order.is_canceled for order in engine.cache.orders()))
        finally:
            engine.dispose()
