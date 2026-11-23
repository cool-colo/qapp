from concurrent.futures import Future
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from task_orchestration.models import Proposal, TaskContext, RunState
from trading_orchestration.components.planners.risk_manager_goal_planner import RiskManagerGoalPlanner
from trading_orchestration.tasks.bootstrap_task import BootstrapTask
from strategies.model_target_planners.base import ModelTargetPlan


class PlanningBoundaryTest(unittest.TestCase):
    def setup_planner(self):
        day = date(2026, 7, 8)
        plan = ModelTargetPlan(trading_date=day, signal_date=day, targets=[], reason="test")
        host = SimpleNamespace(
            _clock_date=lambda: day, _execution_activity=1, _target_version="v1",
            config=SimpleNamespace(risk_manager_timeout_secs=10),
            planning_context_builder=SimpleNamespace(build=Mock(return_value="frozen")),
            prepare_target_planning=Mock(return_value=("request", [], None)),
            capture_planning_inputs=Mock(return_value={}), archive_planning_attempt=Mock(),
            _async_scheduler=SimpleNamespace(has_running_loop=False),
            _target_planner=SimpleNamespace(plan=Mock(return_value=plan)),
            replanning_orders=Mock(return_value=[]), finish_target_planning=Mock(return_value=plan),
            _last_planning_error="optimizer unavailable")
        bridge = SimpleNamespace(host=host, install_model=Mock(), wake_event=Mock())
        return host, bridge, RiskManagerGoalPlanner(bridge), TaskContext(1, 1, "run-1", 1, Proposal("test"))

    def test_committed_before_install_and_parent_is_preserved(self):
        host, bridge, planner, ctx = self.setup_planner()
        actions = []
        host.archive_planning_attempt.side_effect = lambda *args: actions.append("archive")
        bridge.install_model.side_effect = lambda plan: actions.append("install")
        self.assertEqual(planner.plan(ctx).state, RunState.SUCCEEDED)
        self.assertEqual(actions, ["archive", "archive", "install"])
        self.assertEqual(bridge.install_model.call_args.args[0].parent_version, "v1")

    def test_persistence_error_never_installs_candidate(self):
        host, bridge, planner, ctx = self.setup_planner()
        host.archive_planning_attempt.side_effect = [None, RuntimeError("storage failed")]
        with self.assertRaisesRegex(RuntimeError, "storage failed"):
            planner.plan(ctx)
        bridge.install_model.assert_not_called()

    def test_execution_change_while_planning_rejects_result(self):
        host, bridge, planner, ctx = self.setup_planner()
        result = host._target_planner.plan.return_value
        def optimize(_request):
            host._execution_activity += 1
            return result
        host._target_planner.plan.side_effect = optimize
        self.assertEqual(planner.plan(ctx).reason, "planning_snapshot_stale")
        bridge.install_model.assert_not_called()

    def test_abort_discards_late_worker_result(self):
        host, bridge, planner, ctx = self.setup_planner()
        future = Future()
        def schedule(coroutine):
            coroutine.close()
            return future
        host._async_scheduler = SimpleNamespace(has_running_loop=True, schedule=schedule)
        self.assertEqual(planner.plan(ctx).state, RunState.WAITING)
        planner.abort()
        future.set_result(host._target_planner.plan.return_value)
        self.assertEqual(planner.plan(ctx).reason, "planning_authorization_expired")
        bridge.install_model.assert_not_called()

    def test_no_plan_preserves_formal_target(self):
        host, bridge, planner, ctx = self.setup_planner()
        host.finish_target_planning.return_value = None
        self.assertEqual(planner.plan(ctx).state, RunState.FAILED)
        bridge.install_model.assert_not_called()

    def test_empty_and_failed_bootstrap_load(self):
        from task_orchestration.models import Decision
        bridge = SimpleNamespace(bootstrap_load=Mock())
        task = BootstrapTask(bridge)
        self.assertEqual(task.advance(None).state, RunState.SUCCEEDED)
        bridge.bootstrap_load.side_effect = RuntimeError("bad archive")
        self.assertEqual(task.advance(None).state, RunState.WAITING)

    def test_manual_execution_retains_explicit_pause_bypass(self):
        from strategies.convergence import ConvergenceTrigger
        from trading_orchestration.tasks.manual_execution_task import ManualExecutionTask
        host = SimpleNamespace(_trading_paused=True, _within_execution_window=lambda: True,
                               _clock_date=lambda: date(2026, 7, 8))
        bridge = SimpleNamespace(host=host, execute=Mock(), install_quantities=Mock(), target_satisfied=lambda: False)
        task = ManualExecutionTask(bridge)
        trigger = ConvergenceTrigger.MANUAL_SELL_ALL
        from trading_orchestration.contexts.manual_target import ManualTarget
        target = ManualTarget((), date(2026, 7, 8), "manual", "manual-v1", trigger)
        ctx = TaskContext(1, 1, "manual-1", 1, Proposal("manual", target))
        self.assertEqual(task.advance(ctx).state, RunState.RUNNING)
        bridge.execute.assert_called_once_with(trigger=trigger)
        self.assertTrue(host._trading_paused)
