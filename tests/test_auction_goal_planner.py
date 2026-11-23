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

    def test_missing_required_price_fails_without_optimizer(self):
        self.planner.plan(self.context())
        self.work[0].set_result({A: self.fields()[A]})
        result = self.planner.plan(self.context())
        self.assertEqual(result.reason, 'invalid_auction_planning_snapshot')
        self.assertEqual(result.evidence['missing_instruments'], [B])
        self.factory.assert_not_called()
        self.assertIn(DAY, self.host.auction_session.failed_dates)

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
        self.host.clock.set_time(timestamp('2026-07-08 09:24:59'))
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

    def test_execution_requires_its_own_fresh_batch(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.ready()
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.assertEqual(prepare.prepare(self.context()).state, RunState.WAITING)
        self.assertEqual(len(self.work), 2)
        self.assertFalse(prepare.ready)
        self.work[1].set_result(self.fields())
        self.assertEqual(prepare.prepare(self.context()).state, RunState.SUCCEEDED)
        self.assertEqual(self.engine.cache.orders(), [])

    def test_execution_validates_targets_not_all_planning_candidates(self):
        self.host._target_date, self.host._target_version = DAY, 'restored'
        self.host._target_quantities = {A: 100}
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        prepare.prepare(self.context())
        self.work[0].set_result({A: self.fields()[A]})
        self.assertEqual(prepare.prepare(self.context()).state, RunState.SUCCEEDED)

    def test_execution_without_a_target_does_not_fetch(self):
        self.host.clock.set_time(timestamp('2026-07-08 09:24:50'))
        prepare = AuctionExecutionPreparation(self.host.orchestration)
        self.assertEqual(prepare.prepare(self.context()).state, RunState.CANCELLED)
        self.fetch.assert_not_called()
