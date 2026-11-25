from concurrent.futures import Future
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from datetime import date

from task_orchestration.models import Decision, RunState, TaskContext, Proposal
from trading_orchestration.components.planners.auction_goal_planner import AuctionGoalPlanner
from trading_orchestration.components.executors.auction_preparation import AuctionExecutionPreparation
from tests.test_opening_auction import replay_fixture, timestamp

DAY = date(2026, 7, 8)
A, B = '000001.SZ.QMT', '000002.SZ.QMT'


class AuctionPlannerPreparationTest(TestCase):
    def setUp(self):
        self.engine, self.host, self.optimizer, _ = replay_fixture()
        self.addCleanup(self.engine.dispose)
        self.host.clock.set_time(timestamp('2026-07-08 09:24:01'))
        self.host._roll_trading_day(DAY)
        self.work = []
        def fetch(source):
            result = Future()
            self.work.append(result)
            return result
        self.fetch = patch.object(self.host, '_schedule_fetch', side_effect=fetch).start()
        self.addCleanup(patch.stopall)
        self.delegate = SimpleNamespace(plan=Mock(return_value=Decision(RunState.SUCCEEDED, 'planned')),
                                        abort=Mock())
        self.factory = Mock(return_value=self.delegate)
        self.planner = AuctionGoalPlanner(self.host.orchestration, self.host.market_snapshots,
                                          self.host.planning_context_builder, self.factory)

    def context(self, epoch=1):
        return TaskContext(self.host.clock.timestamp_ns(), 1, 'auction-test', epoch, Proposal('test'))

    def fields(self):
        return {A: {'last_price': 10, 'last_close': 10, 'ts_event': timestamp('2026-07-08 09:24:00')},
                B: {'last_price': 20, 'last_close': 20, 'ts_event': timestamp('2026-07-08 09:24:00')}}

    def ready(self):
        self.assertEqual(self.planner.plan(self.context()).state, RunState.WAITING)
        self.work[0].set_result(self.fields())
        return self.planner.plan(self.context())

    def test_waits_for_complete_batch_then_passes_frozen_context_to_delegate(self):
        self.planner.plan(self.context())
        self.planner.plan(self.context())
        self.assertEqual(len(self.work), 1)
        self.factory.assert_not_called()
        self.work[0].set_result(self.fields())
        result = self.planner.plan(self.context())
        self.assertEqual(result.state, RunState.SUCCEEDED)
        self.factory.assert_called_once_with(self.planner.snapshot)
        self.assertEqual(self.planner.snapshot.instruments[A].price, 10)
        self.host.market_snapshots.publish({A: {'last_price': 99}}, 'later')
        self.assertEqual(self.planner.snapshot.instruments[A].price, 10)
        self.assertEqual(self.engine.cache.orders(), [])

    def test_missing_candidate_price_is_delegated_to_shared_planning(self):
        self.planner.plan(self.context())
        self.work[0].set_result({A: self.fields()[A]})
        result = self.planner.plan(self.context())
        self.assertEqual(result.state, RunState.SUCCEEDED)
        self.factory.assert_called_once_with(self.planner.snapshot)
        self.assertNotIn(B, self.planner.snapshot.instruments)
        self.assertNotIn(DAY, self.host.auction_session.failed_dates)

    def test_empty_snapshot_still_fails_without_optimizer(self):
        self.planner.plan(self.context())
        self.work[0].set_result({})
        result = self.planner.plan(self.context())
        self.assertEqual(result.state, RunState.FAILED)
        self.assertEqual(result.reason,
                         'auction_planning_market_failed: whole-market snapshot is empty or invalid')
        self.factory.assert_not_called()

    def test_suspended_missing_price_is_permitted(self):
        self.host._suspended_by_date[DAY] = {'000002.SZ'}
        self.planner.plan(self.context())
        self.work[0].set_result({A: self.fields()[A]})
        self.assertEqual(self.planner.plan(self.context()).state, RunState.SUCCEEDED)

    def test_failed_fetch_is_not_reissued(self):
        self.planner.plan(self.context())
        self.work[0].set_exception(ConnectionError('unavailable'))
        for _ in range(2):
            self.assertEqual(self.planner.plan(self.context()).state, RunState.FAILED)
        self.assertEqual(len(self.work), 1)
        self.factory.assert_not_called()

    def test_late_result_cannot_plan_after_cutoff(self):
        self.planner.plan(self.context())
        self.host.clock.set_time(timestamp('2026-07-08 09:25:00'))
        self.work[0].set_result(self.fields())
        self.assertEqual(self.planner.plan(self.context()).state, RunState.EXPIRED)
        self.factory.assert_not_called()

    def test_abort_cancels_consumer_without_publishing_late_snapshot(self):
        self.planner.plan(self.context())
        self.planner.abort()
        self.work[0].set_result(self.fields())
        self.assertEqual(dict(self.host.market_snapshots.latest_snapshot()), {})
        self.assertEqual(self.planner.plan(self.context()).state, RunState.CANCELLED)
        self.factory.assert_not_called()

    def test_owner_change_does_not_install_ready_result(self):
        self.planner.plan(self.context())
        self.work[0].set_result(self.fields())
        self.assertEqual(self.planner.plan(self.context(epoch=2)).state, RunState.CANCELLED)
        self.factory.assert_not_called()

    def test_existing_target_is_reused_without_an_optimizer_call(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.assertEqual(self.ready().reason, 'reuse_current_target')
        self.factory.assert_not_called()
        self.assertIn(DAY, self.host.auction_session.frozen_dates)
        self.assertEqual(self.host.auction_session.ranks, {A: 2, B: 1})

    def test_new_day_cannot_reuse_previous_day_batch(self):
        self.planner.plan(self.context())
        self.work[0].set_result(self.fields())
        self.host.clock.set_time(timestamp('2026-07-09 09:24:01'))
        self.host._trading_dates = [*self.host._trading_dates, date(2026, 7, 9)]
        self.assertEqual(self.planner.plan(self.context()).reason, 'auction_planning_market_wrong_date')
        self.factory.assert_not_called()

    def test_execution_reuses_cached_prices_without_another_batch(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.ready()
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        result = prepare.prepare(self.context())
        self.assertEqual(result.state, RunState.SUCCEEDED)
        self.assertEqual(result.evidence['prepared_at_ns'], self.host.clock.timestamp_ns())
        self.assertEqual(len(self.work), 1)
        self.assertTrue(prepare.ready)
        self.assertEqual(self.engine.cache.orders(), [])

    def test_execution_preparation_does_not_require_all_planning_candidate_prices(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100}
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        self.host.market_snapshots.publish({A: self.fields()[A]}, 'test')
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.assertEqual(prepare.prepare(self.context()).state, RunState.SUCCEEDED)
        self.fetch.assert_not_called()

    def test_missing_execution_prices_do_not_block_preparation_or_fetch(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100}
        self.host.clock.set_time(timestamp('2026-07-08 09:24:58'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        with patch.object(self.host, '_select_opening_auction_reference') as select:
            result = prepare.prepare(self.context())
        select.assert_not_called()
        self.assertEqual((result.state, result.reason),
                         (RunState.SUCCEEDED, 'auction_execution_ready'))
        self.assertNotIn(DAY, self.host.auction_session.failed_dates)
        self.assertIn(DAY, self.host.auction_session.execution_ready_dates)
        self.fetch.assert_not_called()
        self.assertEqual(self.engine.cache.orders(), [])

    def test_missing_price_for_a_non_target_holding_does_not_block_preparation(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100}
        self.host.market_snapshots.publish({A: self.fields()[A]}, 'test')
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        with patch.object(self.host, '_held_instrument_ids', return_value={B}):
            result = prepare.prepare(self.context())
        self.assertEqual(result.state, RunState.SUCCEEDED)
        self.fetch.assert_not_called()

    def test_execution_exempts_suspended_targets_with_missing_prices(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100, B: 100}
        self.host._suspended_by_date[DAY] = {'000002.SZ'}
        self.host.market_snapshots.publish({A: self.fields()[A]}, 'test')
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.assertEqual(prepare.prepare(self.context()).state, RunState.SUCCEEDED)
        self.fetch.assert_not_called()

    def test_execution_retains_pause_and_reconciliation_gates(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100}
        self.host.market_snapshots.publish({A: self.fields()[A]}, 'test')
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.host._trading_paused = True
        self.assertEqual(prepare.prepare(self.context()).reason, 'user_paused')
        self.host._trading_paused = False
        with patch.object(self.host, '_awaiting_pre_open_reconcile', return_value=True):
            self.assertEqual(prepare.prepare(self.context()).reason, 'awaiting_pre_open_reconciliation')
        self.assertFalse(prepare.ready)
        self.fetch.assert_not_called()

    def test_execution_without_a_target_does_not_fetch(self):
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.assertEqual(prepare.prepare(self.context()).state, RunState.CANCELLED)
        self.fetch.assert_not_called()
