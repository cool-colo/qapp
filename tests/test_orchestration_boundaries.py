"""Behavioral checks for framework-owned cleanup and complete execution cycles."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from msgspec.structs import replace as config_replace

from task_orchestration.components.processes.task_process import BusinessTask
from task_orchestration.core.orchestrator import TaskOrchestrator
from task_orchestration.models import Decision, Proposal, RunState, TaskSpec
from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
from tests.test_opening_auction import replay_fixture, timestamp


class Environment:
    def __init__(self):
        self.orders = []
        self.can_cancel = True
        self.fail = False
        self.trace = []
        self.instances = 0

    def stabilizer(self, scope):
        self.instances += 1
        return self

    def stabilize(self, context):
        self.trace.append(("clean", context.run_id))
        if self.fail:
            raise RuntimeError("temporarily unavailable")
        if self.orders:
            return Decision(RunState.WAITING,
                            "awaiting_terminal_orders" if self.can_cancel else "cancellation_window_closed",
                            {"orders": list(self.orders)})
        return Decision(RunState.SUCCEEDED, "clean")


class Work(BusinessTask):
    def __init__(self, environment, *, done=False):
        self.environment = environment
        self.done = done
        self.contexts = []
        self.interrupted = 0
        self.released = 0
        self.request_clean = False
        self.release_error = False

    def advance(self, context):
        self.contexts.append(context)
        self.environment.trace.append(("work", context.run_id))
        return Decision(RunState.SUCCEEDED if self.done else RunState.RUNNING, "business",
                        requires_stable=self.request_clean)

    def interrupt(self, context):
        self.interrupted += 1
        self.environment.trace.append(("interrupt", context.run_id))

    def after_stable(self, context):
        if self.release_error:
            raise RuntimeError("release failed")
        self.released += 1
        self.request_clean = False
        self.environment.trace.append(("release", context.run_id))


class FrameworkBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.env = Environment()
        self.engine = TaskOrchestrator(stabilizer_factory=self.env.stabilizer)
        self.engine.register(TaskSpec("boot", 1000, preemptible=False),
                             lambda: Work(self.env, done=True), bootstrap=True)
        self.engine.start(0)

    def add(self, name, priority=1):
        work = Work(self.env)
        factory = Mock(return_value=work)
        self.engine.register(TaskSpec(name, priority), factory)
        return work, factory

    def test_first_entry_and_idle_readmission_require_clean_state(self):
        work, factory = self.add("task")
        self.env.orders = ["inflight-submission"]
        self.engine.submit("task", Proposal("one"))
        self.engine.tick(1)
        factory.assert_not_called()
        state = self.engine.snapshot()["runs"][-1]
        self.assertEqual((state["phase"], state["started"]), ("preparing", False))
        self.env.orders.clear()
        self.engine.tick(2)
        self.assertEqual(len(work.contexts), 1)
        work.done = True
        self.engine.tick(3)
        self.env.orders = ["unexpected-order-while-idle"]
        self.engine.submit("task", Proposal("two"))
        self.engine.tick(4)
        self.assertEqual(factory.call_count, 1)

    def test_non_cancel_window_freezes_old_task_and_never_adopts_orders(self):
        old, _ = self.add("old")
        new, factory = self.add("new", 2)
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        self.env.orders, self.env.can_cancel = ["old-order"], False
        self.engine.submit("new", Proposal("new"))
        for now in (2, 3, 4):
            self.engine.tick(now)
        self.assertEqual(len(old.contexts), 1)
        factory.assert_not_called()
        self.assertEqual(old.interrupted, 1)
        self.assertEqual(self.engine.owner_task(), "old")
        self.env.can_cancel = True
        self.engine.tick(5)  # A cancellation request is not a terminal report.
        factory.assert_not_called()
        self.env.orders.clear()
        self.engine.tick(6)
        self.assertEqual(self.engine.owner_task(), "new")
        self.assertEqual(len(new.contexts), 1)

    def test_rearbitrates_after_cleanup_and_does_not_start_expired_candidate(self):
        old, _ = self.add("old")
        expired, expired_factory = self.add("expired", 2)
        winner, _ = self.add("winner", 3)
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        self.env.orders = ["order"]
        self.engine.submit("expired", Proposal("expire", expires_ns=3))
        self.engine.tick(2)
        self.engine.submit("winner", Proposal("latest"))
        self.engine.tick(3)
        self.env.orders.clear()
        self.engine.tick(4)
        expired_factory.assert_not_called()
        self.assertEqual(self.engine.owner_task(), "winner")
        self.assertEqual(len(old.contexts), 1)

    def test_disabled_candidate_in_preparation_never_runs(self):
        _, factory = self.add("candidate")
        self.env.orders = ["order"]
        self.engine.submit("candidate", Proposal("a"))
        self.engine.tick(1)
        self.engine.set_enabled("candidate", False)
        self.env.orders.clear()
        self.engine.tick(2)
        factory.assert_not_called()
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "cancelled")

    def test_same_task_can_request_clean_state_without_latching_a_business_outcome(self):
        work, factory = self.add("task")
        self.engine.submit("task", Proposal("a"))
        work.request_clean = True
        self.engine.tick(1)
        old_context = work.contexts[-1]
        self.env.orders = ["corrective-order"]
        self.engine.tick(2)
        self.assertFalse(self.engine.owns(old_context))
        self.assertIsNone(self.engine.snapshot()["runs"][-1]["final_result"])
        self.env.orders.clear()  # Includes any fills received during cancellation.
        self.engine.tick(3)
        self.assertEqual(len(work.contexts), 2)
        self.assertEqual(work.released, 1)
        self.assertEqual(factory.call_count, 1)
        self.assertNotEqual(work.contexts[-1].owner_epoch, old_context.owner_epoch)
        self.assertTrue(self.engine.owns(work.contexts[-1]))

    def test_failed_release_rechecks_state_before_admission(self):
        old, _ = self.add("old")
        _, factory = self.add("next", 2)
        self.engine.submit("old", Proposal("a"))
        self.engine.tick(1)
        old.release_error = True
        self.engine.submit("next", Proposal("b"))
        self.engine.tick(2)
        factory.assert_not_called()
        self.env.orders = ["late-discovered-order"]
        old.release_error = False
        self.engine.tick(3)
        factory.assert_not_called()
        self.env.orders.clear()
        self.engine.tick(4)
        factory.assert_called_once()

    def test_reentrant_events_evaluate_only_after_whole_execution_loop(self):
        events = []
        engine = self.engine
        class Trigger:
            def evaluate(self, now_ns):
                events.append(("detect", now_ns))
                return None
        class Loop(Work):
            def advance(self, context):
                for item in range(3):
                    events.append(("item", item))
                    if context.now_ns == 1:
                        engine.tick(2)
                return super().advance(context)
        work = Loop(self.env)
        engine.register(TaskSpec("loop", 1, interval_ns=1), lambda: work, Trigger())
        engine.submit("loop", Proposal("a"))
        engine.tick(1)
        self.assertEqual(events, [("detect", 1), ("item", 0), ("item", 1), ("item", 2),
                                  ("detect", 2), ("item", 0), ("item", 1), ("item", 2)])


class BridgeBoundaryTest(unittest.TestCase):
    def test_correction_cannot_preempt_normal_execution_and_yields_to_it(self):
        with TemporaryDirectory() as directory:
            defaults = Path(directory) / 'tasks.yaml'
            defaults.write_text('schema_version: 2\nexposure:\n  enabled: true\n')
            for path in (defaults, Path('configs/task_orchestration.yaml')):
                with self.subTest(path=path):
                    config = OrchestrationConfig.load(str(path))
                    environment = Environment()
                    engine = TaskOrchestrator(stabilizer_factory=environment.stabilizer)
                    engine.register(TaskSpec('bootstrap', 1000, preemptible=False),
                                    lambda: Work(environment, done=True), bootstrap=True)
                    normal, correction = Work(environment), Work(environment)
                    correction_factory = Mock(return_value=correction)
                    engine.register(config.tasks['normal_execution'], lambda: normal)
                    engine.register(config.tasks['exposure_correction'], correction_factory)
                    engine.start(0)
                    engine.tick(0)
                    engine.submit('normal_execution', Proposal('normal'))
                    engine.tick(1)
                    engine.submit('exposure_correction', Proposal('correction'))
                    engine.tick(2)
                    self.assertEqual(engine.owner_task(), 'normal_execution')
                    self.assertEqual(normal.interrupted, 0)
                    correction_factory.assert_not_called()
                    normal.done = True
                    engine.tick(3)
                    engine.tick(4)
                    self.assertEqual(engine.owner_task(), 'exposure_correction')
                    normal.done = False
                    engine.submit('normal_execution', Proposal('next_normal'))
                    engine.tick(5)
                    self.assertEqual(engine.owner_task(), 'normal_execution')
                    self.assertEqual(correction.interrupted, 1)

    def test_event_and_control_callbacks_only_queue_until_next_cycle(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\n")
            replay, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            bridge = strategy.orchestration
            env = Environment()
            bridge.engine = TaskOrchestrator(stabilizer_factory=env.stabilizer)
            bridge.engine.register(TaskSpec("boot", 1000), lambda: Work(env, done=True), bootstrap=True)
            observations = []
            class Loop(Work):
                def advance(self, context):
                    for index in range(3):
                        bridge.wake()
                        if index == 0:
                            bridge.request("next")
                            bridge.submit_control("pause", {})
                        bridge._tick()  # Simulate a synchronous nested callback.
                        observations.append((index, bridge.engine.paused, bridge.engine.owner_task()))
                    return super().advance(context)
            work = Loop(env)
            next_work = Work(env)
            bridge.engine.register(TaskSpec("loop", 1), lambda: work)
            bridge.engine.register(TaskSpec("next", 2), lambda: next_work)
            try:
                strategy.clock.set_time(timestamp("2026-07-08 09:23:00"))
                bridge.start()
                receipt = bridge.request("loop")
                self.assertFalse(work.contexts)
                bridge._tick()
                self.assertEqual(observations, [(0, False, "loop"), (1, False, "loop"), (2, False, "loop")])
                self.assertFalse(next_work.contexts)
                self.assertIsNotNone(bridge.control_results()[receipt]["run_id"])
                bridge._tick()
                self.assertTrue(bridge.engine.paused)
                self.assertFalse(next_work.contexts)
                self.assertEqual(len(work.contexts), 1)
            finally:
                bridge.stop()
                replay.dispose()


class PermissionAndConfigurationTest(unittest.TestCase):
    def test_configuration_requires_public_cleanup_component(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            for content in ("schema_version: 1\n",
                            "schema_version: 2\nexposure_components:\n  stabilizer: cancel_reconcile\n"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    OrchestrationConfig.load(str(path))
            path.write_text("schema_version: 2\ncleanup_component: cancel_reconcile\n")
            config = OrchestrationConfig.load(str(path))
            self.assertEqual(config.cleanup_component, "cancel_reconcile")
            self.assertNotIn("stabilizer", config.exposure_components)

    def test_cleanup_reconciliation_switch_defaults_true_and_rejects_non_booleans(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\n")
            self.assertTrue(OrchestrationConfig.load(str(path)).cleanup_reconciliation_enabled)
            for literal, expected in (("true", True), ("false", False)):
                path.write_text(f"schema_version: 2\ncleanup_reconciliation_enabled: {literal}\n")
                self.assertIs(OrchestrationConfig.load(str(path)).cleanup_reconciliation_enabled, expected)
                replay, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                    c, task_orchestration_config=str(path)))
                try:
                    cleaner = strategy.orchestration.components.build("stabilizer", "cancel_reconcile")
                    self.assertIs(cleaner.reconciliation_enabled, expected)
                    self.assertIs(strategy.orchestration._configuration_view['cleanup_reconciliation_enabled'],
                                  expected)
                finally:
                    replay.dispose()
            for literal in ('"false"', '0', 'null', '[]'):
                path.write_text(f"schema_version: 2\ncleanup_reconciliation_enabled: {literal}\n")
                with self.assertRaisesRegex(ValueError, 'cleanup_reconciliation_enabled must be boolean'):
                    OrchestrationConfig.load(str(path))

    def test_framework_phase_enforces_cleanup_and_release_permissions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\n")
            replay, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            bridge, case = strategy.orchestration, self
            phases = []
            class Cleaner:
                def stabilize(self, context):
                    phases.append(bridge.engine.operation_phase())
                    with bridge.operations():
                        bridge.reject_unowned_operation("cancel")
                        for operation in ("submit", "modify"):
                            with case.assertRaises(RuntimeError):
                                bridge.reject_unowned_operation(operation)
                    return Decision(RunState.SUCCEEDED)
            class Probe(BusinessTask):
                def advance(self, context):
                    phases.append(bridge.engine.operation_phase())
                    with bridge.operations():
                        for operation in ("submit", "modify", "cancel"):
                            bridge.reject_unowned_operation(operation)
                    return Decision(RunState.SUCCEEDED)
                def interrupt(self, context):
                    phases.append(bridge.engine.operation_phase())
                    with case.assertRaises(RuntimeError), bridge.operations():
                        pass
                def after_stable(self, context):
                    phases.append(bridge.engine.operation_phase())
                    with bridge.operations():
                        for operation in ("submit", "modify", "cancel"):
                            with case.assertRaises(RuntimeError):
                                bridge.reject_unowned_operation(operation)
            bridge.engine = TaskOrchestrator(stabilizer_factory=lambda _scope: Cleaner())
            bridge.engine.register(TaskSpec("boot", 1000), Probe, bootstrap=True)
            try:
                bridge.engine.start(0)
                self.assertEqual(phases, ["cleanup", "execute", "interrupt", "cleanup", "release"])
                self.assertEqual(bridge.engine.lifecycle, "ready")
                with self.assertRaises(RuntimeError), bridge.operations():
                    pass
                from strategies.convergence import ConvergenceTrigger
                with patch.object(bridge, "target_satisfied", side_effect=AssertionError("inline assessment")):
                    bridge.execution_requested(ConvergenceTrigger.ORDER_BOOK_DEPTH)
            finally:
                replay.dispose()
