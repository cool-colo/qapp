from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from msgspec.structs import replace as config_replace
from nautilus_trader.model.enums import OrderSide

from strategies.strategy_params import StrategyParams
from strategies.target_quantities import DepthBookSnapshot
from tests.test_opening_auction import FixedPlanner, replay_fixture, timestamp


class ExecutionWindowsTest(unittest.TestCase):
    def setUp(self):
        self.params = StrategyParams.from_yaml('configs/strategy_topn_003.yaml')
        self.engine, self.host, _, _ = replay_fixture(
            auction_enabled=False, transform_config=self.execution_config)
        self.addCleanup(self.engine.dispose)

    def execution_config(self, config):
        return config_replace(
            config, execution_windows=self.params.execution_windows,
            exchange_execution_windows=self.params.exchange_execution_windows,
            trading_windows=self.params.trading_windows,
            exchange_trading_windows=self.params.exchange_trading_windows,
            cancel_windows=self.params.cancel_windows,
            exchange_cancel_windows=self.params.exchange_cancel_windows,
            submission_book_max_age_secs=self.params.submission_book_max_age_secs,
            cancel_reconverge_delay_secs=0,
        )

    def set_market_time(self, clock_time, event_time):
        self.host.clock.set_time(timestamp(f'2026-07-08 {clock_time}'))
        self.host._last_market_data_ts_event = timestamp(f'2026-07-08 {event_time}')

    def test_execution_closes_at_lunch_and_requires_afternoon_event_to_resume(self):
        cases = [
            ('09:59:59', '09:59:59', False),
            ('10:00:00', '10:00:00', True),
            ('11:29:59.999999999', '11:29:59', True),
            ('11:30:00', '11:29:59', False),
            ('11:30:01', '11:29:59', False),
            ('12:00:00', '11:29:59', False),
            ('13:00:00', '11:29:59', False),
            ('13:00:00', '13:00:00', True),
            ('14:09:59.999999999', '14:09:59', True),
            ('14:10:00', '14:09:59', False),
        ]
        for clock_time, event_time, allowed in cases:
            with self.subTest(clock=clock_time, event=event_time):
                self.set_market_time(clock_time, event_time)
                self.assertEqual(self.host._within_execution_window(), allowed)
        self.set_market_time('11:30:01', '11:29:59')
        self.assertFalse(self.host._within_cancel_window())

    def test_order_quote_at_execution_end_is_rejected(self):
        iid = self.host.config.instrument_ids[0]
        for clock_time, event_time, allowed in [
            ('11:29:59.999999999', '11:29:59.999999999', True),
            ('11:30:00', '11:30:00', False),
            ('14:10:00', '14:10:00', False),
            ('13:00:00', '13:00:00', True),
        ]:
            with self.subTest(clock=clock_time, event=event_time):
                self.set_market_time(clock_time, event_time)
                stamp = self.host._last_market_data_ts_event
                self.host._depth_books[str(iid)] = DepthBookSnapshot(
                    bids=((10, 100),), asks=((10.01, 100),), ts_event=stamp, ts_init=stamp)
                context = self.host._build_price_context(
                    self.host.cache.instrument(iid), iid, OrderSide.BUY, Decimal(1000))
                valid, reason, _ = self.host._execution_book_validation(
                    context, max_age_secs=self.params.submission_book_max_age_secs,
                    purpose='order submission')
                self.assertEqual(valid, allowed)
                if not allowed:
                    self.assertEqual(reason, 'outside_execution_window')

    def test_correction_profile_cannot_submit_at_lunch_boundary(self):
        for windows in ('09:35-11:30', '09:35-14:10'):
            for clock_time, allowed in [('11:29:59.999999999', True),
                                        ('11:30:00', False), ('11:30:01', False)]:
                with self.subTest(windows=windows, clock=clock_time):
                    self.set_market_time(clock_time, '11:29:59')
                    self.assertEqual(self.host.orchestration.in_windows(windows), allowed)

    def test_replay_skips_fresh_morning_quotes_during_lunch_and_resumes_in_afternoon(self):
        def lunch_rows(rows):
            morning = [r for r in rows if r.ts_init <= timestamp('2026-07-08 09:30:00')]
            base = [r for r in morning if r.ts_init == timestamp('2026-07-08 09:30:00')]
            arrivals = [('11:30:00', '11:29:59'), ('11:30:01', '11:29:59'),
                        ('13:00:00', '11:29:59'), ('13:00:01', '13:00:01'),
                        ('13:00:02', '13:00:02')]
            return morning + [replace(r, ts_init=timestamp(f'2026-07-08 {arrival}'),
                                      ts_event=timestamp(f'2026-07-08 {event}'))
                              for arrival, event in arrivals for r in base]

        class LargerTargetsPlanner(FixedPlanner):
            def plan(self, request):
                plan = super().plan(request)
                return replace(plan, targets=[replace(t, quantity=t.quantity * 2) for t in plan.targets])

        engine, host, _, _ = replay_fixture(
            auction_enabled=False, continuous=True, starting_cash=100000,
            continuous_buy_min_notional=self.params.continuous_buy_min_notional,
            transform_rows=lunch_rows, transform_config=self.execution_config)
        self.addCleanup(engine.dispose)
        host._target_planner = LargerTargetsPlanner()
        engine.run()
        orders = engine.cache.orders()
        self.assertTrue(orders)
        self.assertTrue(all(order.side == OrderSide.BUY for order in orders))
        self.assertEqual({order.ts_init for order in orders}, {timestamp('2026-07-08 13:00:01')})
        errors = [r for r in host.orchestration.engine.records
                  if r.action in {'COMPONENT_ERROR', 'TRIGGER_ERROR'}]
        self.assertEqual(errors, [])
