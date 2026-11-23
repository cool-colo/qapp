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
        from tests.orchestration_helpers import start_runtime
        self.engine, self.strategy, self.planner, _ = replay_fixture(
            auction_enabled=False, transform_config=lambda c: with_schedule(c, ('10:00', '14:00')))
        self.addCleanup(self.engine.dispose)
        self.strategy.clock.set_time(timestamp('2026-07-08 09:59:55'))
        self.strategy._target_date = DAY
        self.strategy._target_version = 'existing'
        start_runtime(self.strategy)
        self.trigger = self.strategy.orchestration.scheduled_triggers['scheduled_replan']

    def test_timer_enqueues_without_condition_evaluation(self):
        from tests.orchestration_helpers import advance_clock
        from unittest.mock import patch
        with patch.object(self.strategy.orchestration, '_tick_once') as tick:
            advance_clock(self.strategy, timestamp('2026-07-08 10:00:00'))
            self.assertTrue(self.strategy.orchestration._commands)
        self.assertFalse(hasattr(self.trigger, 'evaluate'))
        self.assertIn('scheduled:scheduled_replan:10:00:00', self.strategy.clock.timer_names)

    def test_business_guards_are_in_the_task(self):
        from task_orchestration.models import Proposal, TaskContext, RunState
        self.strategy.set_auto_replan_enabled(False)
        task = self.strategy.orchestration.engine._bindings['scheduled_replan'].factory()
        result = task.advance(TaskContext(timestamp('2026-07-08 10:00:00'), 1, 'test', 1, Proposal('test')))
        self.assertEqual(result.state, RunState.CANCELLED)
        self.assertEqual(self.planner.requests, [])

    def test_restart_only_schedules_future_times(self):
        self.strategy.clock.set_time(timestamp('2026-07-08 13:00:00'))
        self.trigger.start()
        self.assertEqual(self.trigger.next_times['10:00:00'].date(), date(2026, 7, 9))
        self.assertEqual(self.trigger.next_times['14:00:00'].date(), DAY)
