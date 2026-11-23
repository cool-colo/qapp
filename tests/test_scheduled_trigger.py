from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import TestCase

from task_orchestration.components.triggers.scheduled_trigger import ScheduledTrigger


def ns(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1e9)


class Clock:
    def __init__(self):
        self.now = ns('2026-07-08 09:00:00')
        self.events = {}

    def now_ns(self):
        return self.now

    def arm(self, name, at_ns, callback):
        self.events[name] = (at_ns, callback)

    def cancel(self, name):
        self.events.pop(name, None)

    def advance(self, stamp):
        self.now = stamp
        pending = sorted((when, name, call) for name, (when, call) in self.events.items() if when <= stamp)
        for _, name, callback in pending:
            self.events.pop(name)
            callback()


class ScheduledTriggerTest(TestCase):
    def setUp(self):
        self.clock, self.events = Clock(), []
        self.trigger = ScheduledTrigger('work', self.clock, 'Asia/Shanghai',
                                        [('09:24:25', '09:24:59'), ('14:00:00', '15:00:00')], self.events.append)

    def test_pushes_at_time_without_evaluate_and_registers_next_day(self):
        self.assertFalse(hasattr(self.trigger, 'evaluate'))
        self.trigger.start()
        self.clock.advance(ns('2026-07-08 09:24:24'))
        self.assertEqual(self.events, [])
        self.clock.advance(ns('2026-07-08 09:24:25'))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].scheduled_at_ns, ns('2026-07-08 09:24:25'))
        self.assertEqual(self.events[0].expires_at_ns, ns('2026-07-08 09:24:59'))
        self.clock.advance(ns('2026-07-08 14:00:00'))
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.trigger.next_times['09:24:25'].day, 9)

    def test_restart_skips_missed_times_but_exact_time_fires(self):
        self.clock.now = ns('2026-07-08 09:24:26')
        self.trigger.start()
        self.assertEqual(self.trigger.next_times['09:24:25'].day, 9)
        self.clock.now = ns('2026-07-08 14:00:00')
        self.trigger.start()
        self.clock.advance(self.clock.now)
        self.assertEqual(len(self.events), 1)

    def test_disabled_late_callback_and_duplicate_cannot_emit(self):
        self.trigger.start()
        _, callback = self.clock.events['scheduled:work:09:24:25']
        self.trigger.stop()
        callback()
        self.assertEqual(self.events, [])
        self.trigger.start()
        self.clock.now = ns('2026-07-08 09:24:25')
        _, current = self.clock.events['scheduled:work:09:24:25']
        current()
        current()
        self.assertEqual(len(self.events), 1)

    def test_delayed_callback_keeps_original_expiry_without_replaying_days(self):
        self.trigger.start()
        self.clock.advance(ns('2026-07-10 16:00:00'))
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[0].expires_at_ns, ns('2026-07-08 09:24:59'))
        self.assertEqual(self.trigger.next_times['09:24:25'].day, 11)
