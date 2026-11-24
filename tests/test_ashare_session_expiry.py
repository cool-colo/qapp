from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

import pandas as pd

from nautilus_trader.common.actor import Actor
from nautilus_trader.model.enums import OrderStatus, TimeInForce

from backtests.time_range import BacktestTimeRange
from tests.test_opening_auction import replay_fixture, timestamp


class SessionProbe(Actor):
    def __init__(self, strategy):
        super().__init__()
        self.strategy = strategy
        self.rows = {}

    def on_start(self):
        for stage, stamp in (('morning_end', '2026-07-08 10:29:59'),
                             ('after_close', '2026-07-08 15:00:01')):
            self.clock.set_time_alert(stage, pd.Timestamp(timestamp(stamp), unit='ns', tz='UTC'),
                callback=lambda _, stage=stage: self.capture(stage))

    def capture(self, stage):
        self.rows[stage] = dict(orders=list(self.strategy.cache.orders_open()),
                               cash=self.strategy._nautilus_free_cash())


class AShareSessionExpiryTest(unittest.TestCase):
    def test_day_orders_expire_without_afternoon_quotes_and_preserve_positions(self):
        engine, strategy, planner, _ = replay_fixture(starting_cash=50000, second_day_exit=True)
        strategy.configure_replay_time_range(BacktestTimeRange.parse(
            '2026-07-08', '2026-07-09', daily_window='09:15-10:30'))
        probe = SessionProbe(strategy)
        engine.add_actor(probe)
        try:
            engine.run(start=timestamp('2026-07-08 09:15'), end=timestamp('2026-07-09 09:26'))
            before, after = probe.rows['morning_end'], probe.rows['after_close']
            self.assertEqual(len(before['orders']), 2)
            self.assertTrue(all(o.time_in_force == TimeInForce.DAY for o in before['orders']))
            self.assertEqual(after['orders'], [])
            self.assertGreater(after['cash'], before['cash'])
            self.assertEqual(after['cash'], Decimal('46990'))
            self.assertTrue(all(o.status == OrderStatus.EXPIRED for o in engine.cache.orders()))
            events = strategy._auction_replay_module.expiration_events
            self.assertEqual(len(events), 2)
            self.assertTrue(all(timestamp('2026-07-08 15:00') < r['time_ns']
                                < timestamp('2026-07-08 15:00:01') for r in events))
            self.assertEqual({h.stock_code: h.quantity for h in planner.requests[1].current_holdings},
                             {'000001.SZ': 100, '000002.SZ': 100})
            self.assertTrue(all(h.can_sell for h in planner.requests[1].current_holdings))
            fills = engine.trader.generate_fills_report()
            self.assertEqual(fills.last_qty.astype(int).sum(), 200)
            self.assertTrue(all(pd.Timestamp(t).tz_convert('Asia/Shanghai').day == 8
                                for t in fills.ts_event))
        finally:
            engine.dispose()

    def test_expiry_waits_for_delayed_closing_result_and_keeps_its_fills(self):
        def closing_rows(rows):
            return rows + [replace(row, kind='closing_auction',
                ts_event=timestamp('2026-07-08 15:00'),
                ts_init=timestamp('2026-07-08 15:00:00.250'))
                for row in rows if row.kind == 'auction']
        engine, strategy, _, _ = replay_fixture(starting_cash=50000, transform_rows=closing_rows)
        try:
            engine.run(end=timestamp('2026-07-08 15:00:31'))
            fills = engine.trader.generate_fills_report()
            closing = fills[[pd.Timestamp(t).value >= timestamp('2026-07-08 15:00')
                             for t in fills.ts_event]]
            self.assertEqual(closing.last_qty.astype(int).sum(), 200)
            self.assertTrue(all(o.status == OrderStatus.EXPIRED for o in engine.cache.orders()))
            self.assertEqual(sum(r['leaves_qty'] for r in strategy._auction_replay_module.expiration_events),
                             Decimal(1100))
            self.assertTrue(all(r['time_ns'] > timestamp('2026-07-08 15:00:00.250')
                                for r in strategy._auction_replay_module.expiration_events))
        finally:
            engine.dispose()
