"""Task cleanup policies must preserve forced boundaries and truthful ownership."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest

from task_orchestration.core.orchestrator import TaskOrchestrator
from task_orchestration.models import Decision, Proposal, RunState, TaskSpec
from tests.test_orchestration_boundaries import Environment, Work


class PolicyWork(Work):
    def __init__(self, environment, *, done=False):
        super().__init__(environment, done=done)
        self.skipped = 0
        self.skip_error = False
        self.outcome = None
        self.on_interrupt = None
        self.interrupt_error = False

    def advance(self, context):
        result = super().advance(context)
        return self.outcome if self.outcome is not None else result

    def interrupt(self, context):
        super().interrupt(context)
        if self.interrupt_error:
            raise RuntimeError("interrupt failed")
        if self.on_interrupt is not None:
            self.on_interrupt()

    def after_cleanup_skipped(self, context):
        self.skipped += 1
        self.environment.trace.append(("skip_release", context.run_id))
        if self.skip_error:
            raise RuntimeError("skipped release failed")


class TaskCleanupPolicyTest(unittest.TestCase):
    def setUp(self):
        self.env = Environment()
        self.engine = TaskOrchestrator(stabilizer_factory=self.env.stabilizer)
        self.engine.register(TaskSpec("boot", 1000, preemptible=False),
                             lambda: PolicyWork(self.env, done=True), bootstrap=True)
        self.engine.start(0)
        self.env.trace.clear()
        self.env.instances = 0

    def add(self, name="task", priority=1, *, entry=True, exit=True):
        work = PolicyWork(self.env)
        factory = Mock(return_value=work)
        self.engine.register(TaskSpec(name, priority, skip_entry_cleanup=entry,
                                     skip_exit_cleanup=exit), factory)
        return work, factory

    def actions(self, name="task"):
        return [r for r in self.engine.records if r.task_id == name]

    def test_all_policy_combinations_only_skip_selected_normal_boundaries(self):
        for entry in (False, True):
            for exit in (False, True):
                with self.subTest(entry=entry, exit=exit):
                    self.setUp()
                    work, _ = self.add(entry=entry, exit=exit)
                    self.engine.submit("task", Proposal("normal"))
                    self.engine.tick(1)
                    self.assertEqual(self.env.instances, int(not entry))
                    self.assertTrue(self.engine.owns(work.contexts[-1]))
                    self.assertEqual(work.contexts[-1].now_ns, 1)
                    work.done = True
                    self.engine.tick(2)
                    self.assertEqual(self.env.instances, int(not entry) + int(not exit))
                    self.assertEqual((work.interrupted, work.released, work.skipped),
                                     (1, int(not exit), int(exit)))
                    skipped = [r for r in self.actions() if r.action == "CLEANUP_SKIPPED"]
                    completed = [r for r in self.actions() if r.action == "CLEANUP_COMPLETED"]
                    self.assertEqual([r.evidence["kind"] for r in skipped],
                                     (["entry"] if entry else []) + (["exit"] if exit else []))
                    self.assertEqual(len(completed), int(not entry) + int(not exit))
                    self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "succeeded")

    def test_entry_skip_does_not_consult_stabilizer_or_claim_stability(self):
        work, _ = self.add()
        self.env.orders = ["unknown-account-order"]
        self.env.fail = True
        self.engine.submit("task", Proposal("normal"))
        self.engine.tick(1)
        self.assertEqual(self.env.instances, 0)
        self.assertEqual(self.env.trace, [("work", work.contexts[-1].run_id)])
        admitted = next(r for r in self.actions() if r.action == "ADMITTED")
        self.assertFalse(admitted.evidence["stable_scope"])
        state = self.engine.snapshot()
        self.assertTrue(state["tasks"]["task"]["skip_entry_cleanup"])
        self.assertTrue(state["tasks"]["task"]["skip_exit_cleanup"])
        self.assertTrue(state["runs"][-1]["cleanup_result"]["evidence"]["cleanup_skipped"])

    def test_forced_exit_never_skips_and_retains_owner_until_stable(self):
        for cause in ("pause", "stop", "cancel", "expiry", "preempt", "failed", "unachievable"):
            with self.subTest(cause=cause):
                self.setUp()
                work, _ = self.add()
                next_work, factory = self.add("next", 2)
                run_id = self.engine.submit("task", Proposal("normal", expires_ns=2 if cause == "expiry" else None))
                self.engine.tick(1)
                old_context = work.contexts[-1]
                self.env.orders = ["old-order"]
                if cause == "pause":
                    self.engine.paused = True
                elif cause == "stop":
                    self.engine.stop()
                elif cause == "cancel":
                    self.engine.cancel(run_id)
                elif cause == "preempt":
                    self.engine.submit("next", Proposal("higher"))
                elif cause in {"failed", "unachievable"}:
                    work.outcome = Decision(RunState(cause), "business_failure")
                self.engine.tick(2)
                self.assertEqual(self.engine.owner_task(), "task")
                self.assertFalse(self.engine.owns(old_context))
                factory.assert_not_called()
                self.assertEqual((work.interrupted, work.skipped), (1, 0))
                self.assertTrue(any(r.action == "WAITING_CLEANUP" for r in self.actions()))
                self.env.orders.clear()
                self.engine.tick(3)
                self.assertEqual(work.released, 1)
                self.assertEqual(work.skipped, 0)
                if cause == "preempt":
                    self.assertTrue(self.engine.owns(next_work.contexts[-1]))
                else:
                    self.assertIsNone(self.engine.owner_task())

    def test_reenter_is_real_cleanup_with_new_epoch_and_no_skip_hook(self):
        work, factory = self.add()
        self.engine.submit("task", Proposal("normal"))
        work.request_clean = True
        self.engine.tick(1)
        old_context = work.contexts[-1]
        self.env.orders = ["order"]
        self.engine.tick(2)
        self.assertFalse(self.engine.owns(old_context))
        self.env.orders.clear()
        self.engine.tick(3)
        self.assertEqual((work.interrupted, work.released, work.skipped), (1, 1, 0))
        factory.assert_called_once()
        self.assertNotEqual(work.contexts[-1].owner_epoch, old_context.owner_epoch)
        self.assertTrue(self.engine.owns(work.contexts[-1]))

    def test_resume_after_completed_pause_cleanup_uses_fresh_entry_policy(self):
        work, factory = self.add()
        self.engine.submit("task", Proposal("normal"))
        self.engine.tick(1)
        old_context = work.contexts[-1]
        self.engine.paused = True
        self.engine.tick(2)
        self.assertIsNone(self.engine.owner_task())
        self.assertEqual(work.released, 1)
        self.env.orders = ["new-order-after-completed-pause"]
        self.engine.paused = False
        self.engine.tick(3)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(len(work.contexts), 2)
        self.assertTrue(self.engine.owns(work.contexts[-1]))
        self.assertFalse(self.engine.owns(old_context))
        self.assertEqual([r.evidence["kind"] for r in self.actions() if r.action == "CLEANUP_SKIPPED"],
                         ["entry", "entry"])

    def test_skipped_entry_cannot_bypass_previous_exit_cleanup(self):
        old, _ = self.add("old", entry=False, exit=False)
        new, factory = self.add("next", 2)
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        self.env.orders = ["old-order"]
        self.engine.submit("next", Proposal("next"))
        for now in (2, 3):
            self.engine.tick(now)
        self.assertEqual(self.engine.owner_task(), "old")
        factory.assert_not_called()
        self.env.orders.clear()
        self.engine.tick(4)
        self.assertEqual(len(new.contexts), 1)
        self.assertEqual(old.released, 1)

    def test_rearbitration_after_skipped_exit_requires_new_candidate_entry_cleanup(self):
        old, _ = self.add("old")
        new, factory = self.add("next", 2, entry=False)
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        old.done = True
        self.env.orders = ["order-left-on-skipped-exit"]
        self.engine.tick(2)
        self.assertEqual(old.skipped, 1)
        self.engine.submit("next", Proposal("next"))
        self.engine.tick(3)
        factory.assert_not_called()
        self.assertEqual(self.engine.snapshot()["runs"][-1]["phase"], "preparing")
        self.env.orders.clear()
        self.engine.tick(4)
        self.assertTrue(self.engine.owns(new.contexts[-1]))

    def test_rearbitration_does_not_pass_skipped_entry_as_stable_to_strict_candidate(self):
        old, old_factory = self.add("old")
        new, new_factory = self.add("next", 2, entry=False)
        def audit(record):
            if record.task_id == "old" and record.action == "CLEANUP_SKIPPED":
                self.engine.submit("next", Proposal("arrived_during_boundary"))
        self.engine._audit = audit
        self.env.orders = ["order"]
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        old_factory.assert_not_called()
        new_factory.assert_not_called()
        self.assertEqual(self.engine.owner_task(), "next")
        self.env.orders.clear()
        self.engine.tick(2)
        self.assertTrue(self.engine.owns(new.contexts[-1]))

    def test_skip_hook_error_forces_real_cleanup_and_preserves_success(self):
        work, _ = self.add()
        run_id = self.engine.submit("task", Proposal("normal"))
        work.done, work.skip_error = True, True
        self.engine.tick(1)
        self.assertEqual(self.engine.owner_task(), "task")
        self.assertEqual(self.engine.snapshot()["runs"][-1]["final_result"]["state"], "succeeded")
        self.env.orders = ["late-order"]
        self.engine.tick(2)
        self.assertEqual((work.skipped, work.interrupted), (1, 1))
        self.assertEqual(self.engine.owner_task(), "task")
        self.env.orders.clear()
        self.engine.tick(3)
        self.assertEqual(work.released, 1)
        self.assertIsNone(self.engine.owner_task())
        self.assertFalse([r for r in self.actions() if r.action == "CLEANUP_SKIPPED" and r.evidence["kind"] == "exit"])
        terminal = next(r for r in self.actions() if r.action == "TERMINATED")
        self.assertEqual((terminal.run_id, terminal.reason, terminal.evidence["state"]),
                         (run_id, "business", "succeeded"))

    def test_rearbitration_checks_deadline_of_candidate_added_during_skipped_boundary(self):
        old, _ = self.add("old")
        _, factory = self.add("next", 2)
        def audit(record):
            if record.task_id == "old" and record.action == "CLEANUP_SKIPPED":
                self.engine.submit("next", Proposal("already_expired", expires_ns=1))
        self.engine._audit = audit
        self.engine.submit("old", Proposal("old"))
        self.engine.tick(1)
        self.assertTrue(self.engine.owns(old.contexts[-1]))
        factory.assert_not_called()
        self.engine.tick(2)
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "expired")
        factory.assert_not_called()

    def test_cancel_during_interrupt_cannot_skip_exit_or_rewrite_success(self):
        work, _ = self.add()
        run_id = self.engine.submit("task", Proposal("normal"))
        work.done = True
        work.on_interrupt = lambda: self.engine.cancel(run_id)
        self.env.orders = ["order"]
        self.engine.tick(1)
        self.assertEqual((work.skipped, work.interrupted), (0, 1))
        self.assertEqual(self.engine.owner_task(), "task")
        self.env.orders.clear()
        self.engine.tick(2)
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "succeeded")

    def test_control_after_skip_decision_still_requires_real_cleanup(self):
        for kind in ("entry", "exit"):
            for control in ("cancel", "expiry"):
                with self.subTest(kind=kind, control=control):
                    self._control_after_skip(kind, control)

    def _control_after_skip(self, kind, control):
        self.setUp()
        work, _ = self.add()
        run_id = self.engine.submit("task", Proposal("normal"))
        if kind == "exit":
            self.engine.tick(1)
            work.done = True
        def audit(record):
            if (record.task_id == "task" and record.action == "CLEANUP_SKIPPED"
                    and record.evidence["kind"] == kind):
                if control == "expiry":
                    self.engine.submit("task", Proposal("expired_during_boundary", expires_ns=2))
                else:
                    self.engine.cancel(run_id)
        self.engine._audit = audit
        self.env.orders = ["unconfirmed-order"]
        self.engine.tick(2)
        self.assertEqual(self.engine.owner_task(), "task")
        self.assertTrue([r for r in self.actions() if r.action == "CLEANUP_REDIRECTED"])
        self.env.orders.clear()
        self.engine.tick(3)
        self.assertIsNone(self.engine.owner_task())
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"],
                         "succeeded" if kind == "exit" else
                         "expired" if control == "expiry" else "cancelled")

    def test_interrupt_error_invalidates_skip_and_retries_before_real_cleanup(self):
        work, _ = self.add()
        self.engine.submit("task", Proposal("normal"))
        work.done, work.interrupt_error = True, True
        self.engine.tick(1)
        self.assertEqual(self.engine.owner_task(), "task")
        work.interrupt_error = False
        self.env.orders = ["late-order"]
        self.engine.tick(2)
        self.assertEqual((work.interrupted, work.skipped), (2, 0))
        self.assertEqual(self.engine.owner_task(), "task")
        self.env.orders.clear()
        self.engine.tick(3)
        self.assertEqual(work.released, 1)
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "succeeded")

    def test_both_release_hooks_restore_exposure_temporary_state(self):
        from trading_orchestration.tasks.exposure_correction_task import ExposureCorrectionTask
        for hook in ("stable", "skipped"):
            with self.subTest(hook=hook):
                executor, replacement, risk = Mock(), Mock(), Mock()
                factory = Mock(side_effect=[executor, replacement])
                task = ExposureCorrectionTask(Mock(), assessor=Mock(), planner=Mock(),
                    executor_factory=factory, risk_planner_factory=Mock(), outcome_policy=Mock())
                task.active_plan, task.risk_planner = Mock(), risk
                task.interrupt(None)
                risk.abort.assert_called_once()
                if hook == "stable":
                    task.after_stable(None)
                else:
                    task.after_cleanup_skipped(None)
                executor.release.assert_called_once_with(None)
                self.assertIsNone(task.active_plan)
                self.assertIsNone(task.risk_planner)
                self.assertIs(task.executor, replacement)

    def test_factory_failure_after_skipped_entry_retains_cleanup_responsibility(self):
        _, factory = self.add()
        factory.side_effect = RuntimeError("constructor failed")
        self.env.orders = ["order"]
        self.engine.submit("task", Proposal("normal"))
        self.engine.tick(1)
        self.assertEqual(self.engine.owner_task(), "task")
        self.engine.tick(2)
        self.assertEqual(self.engine.owner_task(), "task")
        self.env.orders.clear()
        self.engine.tick(3)
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "failed")
        self.assertIsNone(self.engine.owner_task())

    def test_expired_queued_skipped_task_never_constructs_or_executes(self):
        _, factory = self.add()
        self.engine.submit("task", Proposal("too_late", expires_ns=1))
        self.engine.tick(1)
        factory.assert_not_called()
        self.assertEqual(self.engine.snapshot()["runs"][-1]["state"], "expired")
        self.assertEqual(self.env.instances, 0)

    def test_bootstrap_rejects_either_skip_flag(self):
        for entry, exit in ((True, False), (False, True), (True, True)):
            engine = TaskOrchestrator(stabilizer_factory=self.env.stabilizer)
            with self.assertRaisesRegex(ValueError, "bootstrap cannot skip cleanup"):
                engine.register(TaskSpec("boot", 1000, skip_entry_cleanup=entry,
                                         skip_exit_cleanup=exit), PolicyWork, bootstrap=True)


class CleanupPolicyConfigurationTest(unittest.TestCase):
    def test_defaults_and_shipped_auction_selection(self):
        from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\n")
            defaults = OrchestrationConfig.load(str(path))
            self.assertTrue(all(not s.skip_entry_cleanup and not s.skip_exit_cleanup
                                for s in defaults.tasks.values()))
        shipped = OrchestrationConfig.load("configs/task_orchestration.yaml")
        self.assertEqual([name for name, s in shipped.tasks.items() if s.skip_entry_cleanup],
                         ["auction_execution"])
        self.assertFalse(any(s.skip_exit_cleanup for s in shipped.tasks.values()))

    def test_only_strict_boolean_flags_are_accepted(self):
        from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
        for key in ("skip_entry_cleanup", "skip_exit_cleanup"):
            for value in ("true", 0, 1, None, [], {}):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "must be boolean"):
                    TaskSpec("task", 1, **{key: value})
            with TemporaryDirectory() as directory:
                path = Path(directory) / "tasks.yaml"
                for literal in ('"true"', '"false"', '0', '1', 'null', '[]'):
                    path.write_text(f"schema_version: 2\ntasks:\n  auction_execution:\n    {key}: {literal}\n")
                    with self.subTest(key=key, literal=literal), self.assertRaisesRegex(ValueError, "must be boolean"):
                        OrchestrationConfig.load(str(path))
                for literal, expected in (("true", True), ("false", False)):
                    path.write_text(f"schema_version: 2\ntasks:\n  auction_execution:\n    {key}: {literal}\n")
                    spec = OrchestrationConfig.load(str(path)).tasks["auction_execution"]
                    self.assertIs(spec.skip_entry_cleanup if key == "skip_entry_cleanup" else spec.skip_exit_cleanup,
                                  expected)
