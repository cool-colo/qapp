from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from nautilus_trader.model.objects import Quantity

from msgspec.structs import replace

from lives.snapshot_recorder import SnapshotRecorder, SnapshotRecorderConfig
from strategies.strategy_params import StrategyParams
from strategies.trading_control import SELL_ALL_REASON
from tests.orchestration_helpers import cycle, start_runtime
from tests.test_opening_auction import replay_fixture, timestamp

DAY = date(2026, 7, 8)
A, B = '000001.SZ.QMT', '000002.SZ.QMT'


class MandatoryOrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, self.planner, _ = replay_fixture(auction_enabled=False)
        self.addCleanup(self.engine.dispose)
        self.strategy.clock.set_time(timestamp('2026-07-08 10:00:00'))
        self.strategy._roll_trading_day(DAY)
        self.strategy.apply_full_tick_snapshot({
            A: {'last_price': 10, 'open': 10, 'last_close': 10},
            B: {'last_price': 20, 'open': 20, 'last_close': 20}}, 'test')
        self.strategy._within_execution_window = Mock(return_value=True)
        self.strategy._within_cancel_window = Mock(return_value=True)
        self.strategy._convergence_suspended = True
        self.strategy.update_target_quantities({}, DAY, 'existing', version='existing')

    def advance(self, count=4):
        for _ in range(count):
            cycle(self.strategy, self.strategy.clock.timestamp_ns() + 500_000_000)

    def start(self):
        start_runtime(self.strategy)

    def test_omitted_path_constructs_required_runtime_and_empty_path_is_invalid(self):
        config = replace(self.strategy.config, task_orchestration_config=StrategyParams().task_orchestration_config)
        self.assertIsNotNone(type(self.strategy)(config).orchestration)
        for value in ('', ' ', None, False):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    StrategyParams(task_orchestration_config=value)
                with self.assertRaises(ValueError):
                    type(self.strategy)(replace(config, task_orchestration_config=value))
        with self.assertRaises(FileNotFoundError):
            type(self.strategy)(replace(config, task_orchestration_config='/tmp/nonexistent-qapp-tasks.yaml'))

    def test_running_strategy_rejects_direct_target_mutation(self):
        self.start()
        with self.assertRaisesRegex(RuntimeError, "active orchestration task"):
            self.strategy.update_target_quantities({A: 100}, DAY, "bypass", version="bypass")
        self.assertEqual(self.strategy._target_version, "existing")

    def test_manual_replan_uses_owner_and_keeps_pause_and_auto_flags(self):
        self.start()
        self.strategy.set_auto_replan_enabled(False)
        self.strategy._trading_paused = True
        self.strategy.request_replanning()
        self.assertEqual(self.planner.requests, [])
        self.advance()
        self.assertEqual(len(self.planner.requests), 1)
        self.assertNotEqual(self.strategy._target_version, 'existing')
        self.assertTrue(self.strategy._trading_paused)
        self.assertFalse(self.strategy._auto_replan_enabled)
        self.assertEqual(self.strategy.orchestration.planning_state()['replan_counts']['manual'], 1)

    def test_no_cancel_window_retains_owner_then_captures_after_late_fill(self):
        self.start()
        orders = [SimpleNamespace(client_order_id='old', is_pending_cancel=False, leaves_qty=Quantity.from_int(100))]
        cancel = Mock()
        with patch.object(self.strategy, 'replanning_orders', side_effect=lambda: list(orders)), \
             patch.object(self.strategy, 'cancel_for_replanning', cancel):
            self.strategy._within_cancel_window.return_value = False
            self.strategy.orchestration.request('scheduled_replan')
            self.advance()
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy.orchestration.engine.owner_task(), 'scheduled_replan')
            cancel.assert_not_called()
            self.strategy._within_cancel_window.return_value = True
            self.advance(1)
            cancel.assert_called_once()
            self.assertEqual(self.planner.requests, [])
            orders.clear()
            self.strategy._execution_activity += 1
            self.strategy.apply_full_tick_snapshot({A: {'last_price': 10.5, 'open': 10, 'last_close': 10}}, 'late')
            self.advance()
        self.assertEqual(len(self.planner.requests), 1)
        attempt = list(self.strategy.planning_attempts.values())[-1]
        self.assertEqual(attempt['status'], 'committed')
        self.assertEqual(attempt['inputs']['context']['instruments'][A]['price'], 10.5)

    def test_failed_cache_cleanup_never_calls_optimizer_and_recovers(self):
        self.start()
        with patch.object(self.strategy, 'replanning_orders', side_effect=RuntimeError('cache unavailable')):
            self.strategy.request_replanning()
            self.advance()
            self.assertEqual(self.planner.requests, [])
            self.assertEqual(self.strategy._target_version, 'existing')
        self.advance()
        self.assertEqual(len(self.planner.requests), 1)

    def test_archive_failure_preserves_target_and_records_failed_task(self):
        self.start()
        def persist(attempt, plan):
            if plan is not None:
                raise RuntimeError('archive unavailable')
        self.strategy.configure_planning_archive(persist)
        self.strategy.request_replanning()
        self.advance()
        self.assertEqual(self.strategy._target_version, 'existing')
        failed = [r for r in self.strategy.orchestration.engine.records if r.action == 'TERMINATED']
        self.assertTrue(any(r.evidence['state'] == 'failed' for r in failed))

    def test_manual_target_is_installed_only_after_cleanup(self):
        self.start()
        orders = [SimpleNamespace(client_order_id='old', is_pending_cancel=False, leaves_qty=Quantity.from_int(100))]
        with patch.object(self.strategy, 'replanning_orders', side_effect=lambda: list(orders)), \
             patch.object(self.strategy, 'cancel_for_replanning'), \
             patch.object(self.strategy.orchestration, 'execute'):
            self.strategy.control_apply_targets({A: Decimal(0)}, DAY, SELL_ALL_REASON)
            self.assertEqual(self.strategy._target_version, 'existing')
            self.advance()
            self.assertEqual(self.strategy._target_version, 'existing')
            orders.clear()
            self.advance()
            self.assertEqual(self.strategy._target_reason, SELL_ALL_REASON)
            self.assertEqual(self.strategy._target_quantities, {A: Decimal(0)})

    def test_newest_manual_request_wins_before_admission(self):
        self.start()
        self.strategy.control_apply_targets({A: Decimal(0)}, DAY, SELL_ALL_REASON)
        self.strategy.control_apply_targets({B: Decimal(0)}, DAY, SELL_ALL_REASON)
        self.advance()
        self.assertEqual(self.strategy._target_quantities, {B: Decimal(0)})
        admitted = [r for r in self.strategy.orchestration.engine.records
                    if r.task_id == "manual_execution" and r.action == "ADMITTED"]
        self.assertEqual(len(admitted), 1)

    def test_snapshot_recorder_only_queues_planning(self):
        writer = Mock()
        recorder = SnapshotRecorder(SnapshotRecorderConfig(account_id='a', trader_id='t'), writer, self.strategy)
        self.engine.add_actor(recorder)
        callback = Mock()
        recorder._ensure_daily_target(DAY, callback)
        self.assertEqual(self.planner.requests, [])
        writer.write_planning_attempt.assert_not_called()
        self.assertEqual(self.strategy._target_version, 'existing')
        self.assertEqual(callback.call_args.args[2]['reason'], 'delegated to task orchestration')

    def test_shutdown_during_entry_never_reactivates_task(self):
        self.start()
        self.strategy.request_replanning()
        self.advance(1)
        self.strategy.orchestration.stop()
        self.advance()
        self.assertEqual(self.planner.requests, [])
        self.assertEqual(self.strategy.orchestration.engine.lifecycle, 'stopping')
        now = self.strategy.clock.timestamp_ns() + 500_000_000
        self.strategy.clock.set_time(now)
        self.strategy.orchestration.engine.tick(now)
        self.assertEqual(self.strategy.orchestration.engine.lifecycle, 'stopped')


class DeferredLimitDetectionTest(unittest.TestCase):
    def test_planning_does_not_consume_late_limit_latches(self):
        from trading_orchestration.components.triggers.price_limit_trigger import PriceLimitTrigger
        policy = SimpleNamespace(collect=Mock(return_value=('blocked',)), validity='validity')
        host = SimpleNamespace(_clock_date=lambda: DAY, _price_limit_replan_triggers=policy)
        bridge = SimpleNamespace(host=host, engine=SimpleNamespace(owner_task=Mock(return_value='manual_replan')),
                                 deadline=lambda: 100)
        trigger = PriceLimitTrigger(bridge)
        self.assertIsNone(trigger.evaluate(1))
        policy.collect.assert_not_called()
        bridge.engine.owner_task.return_value = None
        self.assertEqual(trigger.evaluate(2).payload, 'validity')
        policy.collect.assert_called_once_with(host, DAY)

    def test_opened_limit_after_planning_does_not_force_replan(self):
        from trading_orchestration.components.triggers.price_limit_trigger import PriceLimitTrigger
        policy = SimpleNamespace(collect=Mock(return_value=()), validity='validity')
        host = SimpleNamespace(_clock_date=lambda: DAY, _price_limit_replan_triggers=policy)
        bridge = SimpleNamespace(host=host, engine=SimpleNamespace(owner_task=lambda: None), deadline=lambda: 100)
        self.assertIsNone(PriceLimitTrigger(bridge).evaluate(2))
