from concurrent.futures import Future
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_orchestration.services.whole_market_snapshot import WholeMarketSnapshotService


class Runtime:
    def __init__(self):
        self.now = int(datetime(2026, 7, 8, 9, 24, 25, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1e9)
        self.work = []
        self.info = Mock()
        self.clock = SimpleNamespace(timer_names=[], cancel_timer=Mock())

    def now_ns(self):
        return self.now

    def local_now(self):
        return datetime.fromtimestamp(self.now / 1e9, ZoneInfo('Asia/Shanghai'))

    def fetch(self, _):
        future = Future()
        self.work.append(future)
        return future

    def dispatch(self, call):
        call()


class SnapshotServiceTest(TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.service = WholeMarketSnapshotService(SimpleNamespace(whole_market_final_refresh_time='15:01'), self.runtime)
        self.service.configure_source(lambda: {})

    def test_each_fresh_request_gets_its_own_batch_and_never_mutates_prior_batch(self):
        first = self.service.request_fresh('planning')
        second = self.service.request_fresh('execution')
        self.assertEqual(len(self.runtime.work), 1)
        self.runtime.work[0].set_result({'A': {'last_price': 10}})
        self.assertEqual(len(self.runtime.work), 2)
        self.assertFalse(second.done())
        self.runtime.now += 1
        self.runtime.work[1].set_result({'A': {'last_price': 11}})
        self.assertEqual(first.result().ticks['A'].last_price, 10)
        self.assertEqual(second.result().ticks['A'].last_price, 11)
        with self.assertRaises(TypeError):
            first.result().ticks['A'] = None

    def test_aborted_request_does_not_publish_or_stop_other_consumers(self):
        first = self.service.request_fresh('planning')
        second = self.service.request_fresh('execution')
        first.cancel()
        self.runtime.work[0].set_result({'A': {'last_price': 10}})
        self.assertEqual(dict(self.service.latest_snapshot()), {})
        self.runtime.work[1].set_result({'A': {'last_price': 11}})
        self.assertEqual(second.result().ticks['A'].last_price, 11)

    def test_failed_batch_does_not_block_the_next_consumer(self):
        first = self.service.request_fresh('planning')
        second = self.service.request_fresh('execution')
        self.runtime.work[0].set_exception(ConnectionError('down'))
        with self.assertRaises(ConnectionError):
            first.result()
        self.runtime.work[1].set_result({'A': {'last_price': 11}})
        self.assertTrue(second.done())

    def test_stop_ignores_late_result(self):
        first = self.service.request_fresh('planning')
        self.service.stop()
        self.runtime.work[0].set_result({'A': {'last_price': 10}})
        self.assertTrue(first.cancelled())
        self.assertEqual(dict(self.service.latest_snapshot()), {})

    def test_empty_batch_is_failure_and_cannot_mark_final_success(self):
        request = self.service.request_fresh('final')
        self.runtime.work[0].set_result({})
        with self.assertRaises(ValueError):
            request.result()
        self.assertEqual(self.service.final_success_dates, set())

    def test_observer_sees_installed_batch_and_periodic_requests_coalesce(self):
        first = self.service.refresh('periodic')
        observed = []
        second = self.service.refresh('periodic', lambda state, _: observed.append((state, self.service.latest_snapshot())))
        self.assertIs(first, second)
        self.runtime.work[0].set_result({'A': {'last_price': 10}})
        self.assertEqual(observed[0][0], 'success')
        self.assertEqual(observed[0][1]['A'].last_price, 10)

    def test_deferred_request_cannot_survive_stop_and_restart(self):
        callbacks = []
        self.runtime.dispatch = callbacks.append
        request = self.service.request_fresh('planning')
        self.service.stop()
        self.service.config.whole_market_full_tick_enabled = False
        self.service.start()
        callbacks.pop()()
        self.assertTrue(request.cancelled())
        self.assertEqual(self.runtime.work, [])

    def test_daily_refresh_rearms_and_old_callbacks_cannot_survive_restart(self):
        alerts = []
        self.runtime.clock.set_time_alert = lambda **kw: alerts.append(kw)
        self.service._daily('FINAL', '15:01', 'final')
        first = alerts[-1]
        self.runtime.now = int(first['alert_time'].value)
        first['callback'](None)
        self.assertEqual(len(alerts), 2)
        self.runtime.work[0].set_result({'A': {'last_price': 10}})
        self.assertTrue(self.service.final_refresh_succeeded(self.runtime.local_now().date()))
        callbacks = []
        self.runtime.dispatch = callbacks.append
        alerts[-1]['callback'](None)
        self.service.stop()
        self.service.config.whole_market_full_tick_enabled = False
        self.service.start()
        callbacks.pop()()
        self.assertEqual(len(alerts), 2)
        self.assertEqual(len(self.runtime.work), 1)

    def test_periodic_refresh_queued_before_stop_cannot_survive_restart(self):
        callbacks = []
        self.runtime.dispatch = callbacks.append
        self.service._periodic(self.service._generation)
        self.service.stop()
        self.service.config.whole_market_full_tick_enabled = False
        self.service.start()
        callbacks.pop()()
        self.assertEqual(self.runtime.work, [])
