from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from msgspec.structs import replace as config_replace

from task_orchestration.components.processes.task_process import BusinessTask
from task_orchestration.core.orchestrator import TaskOrchestrator
from task_orchestration.models import Decision, Proposal, RunState, TaskSpec
from tests.test_opening_auction import replay_fixture
from trading_orchestration.adapters.scheduler_description import describe_scheduler
from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
from web.app import scheduler_overview
from web.scheduler_report import build_scheduler_report


class RejectLock:
    def __enter__(self):
        raise AssertionError("a reader acquired an execution lock")

    def __exit__(self, *_args):
        return False


class Process(BusinessTask):
    def __init__(self, *, done=False, clean=True):
        self.done, self.clean = done, clean

    def advance(self, context):
        return Decision(RunState.SUCCEEDED if self.done else RunState.RUNNING,
                        "target_achieved" if self.done else "target_remaining",
                        {"target": {"version": "v1"}})

    def interrupt(self, context):
        pass

    def after_stable(self, context):
        pass


class ObservationStabilizer:
    def __init__(self, engine):
        self.engine = engine

    def stabilize(self, context):
        process = self.engine._runs[context.run_id].process
        clean = process is None or process.clean
        return Decision(RunState.SUCCEEDED if clean else RunState.WAITING,
                        "execution_stable" if clean else "cancelling_existing_orders",
                        {"orders": [] if clean else ["o1"]})


class PublishedSchedulerObservationTest(unittest.TestCase):
    def test_real_bridge_reads_published_prefix_without_execution_or_locks(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\n")
            engine, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            bridge = strategy.orchestration
            try:
                bridge.engine.submit("manual_replan", Proposal("first"))
                # Model a completed publication followed by an owner appending a
                # new action before its next publication.
                bridge._published_state = bridge.engine.snapshot()
                bridge.engine.submit("manual_execution", Proposal("not-published"))
                original = deepcopy(bridge._published_state)
                with patch.object(bridge.engine, "_lock", RejectLock()), \
                        patch.object(bridge, "_command_lock", RejectLock()), \
                        patch.object(strategy, "_trading_action_lock", RejectLock()), \
                        patch.object(bridge.engine, "tick", side_effect=AssertionError("reader ticked")), \
                        patch.object(bridge, "execution_state", side_effect=AssertionError("reader queried executor")):
                    state = bridge.state()
                    actions = bridge.published_actions(limit=1000)
                    self.assertEqual([a["sequence"] for a in actions], [1])
                    self.assertEqual(bridge.published_actions(after_sequence=1), [])
                    self.assertEqual(bridge.published_actions(after_sequence=100), [])
                    state["runs"][0]["reason"] = "reader mutation"
                    state["configuration"]["tasks"]["normal_execution"]["enabled"] = False
                    actions[0]["evidence"]["reader"] = {"nested": True}
                    self.assertEqual(bridge._published_state, original)
                    self.assertTrue(bridge.state()["configuration"]["tasks"]["normal_execution"]["enabled"])
                    self.assertNotIn("reader", bridge.published_actions()[0]["evidence"])
                    for params in ({"limit": True}, {"limit": 1001}, {"after_sequence": -1},
                                   {"after_sequence": False}):
                        with self.subTest(params=params), self.assertRaises(ValueError):
                            bridge.published_actions(**params)
            finally:
                engine.dispose()


class SchedulerReportTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "tasks.yaml"
        path.write_text("schema_version: 2\nexposure:\n  enabled: true\n  priority: 350\n"
                        "tasks:\n  exposure_correction:\n    enabled: false\n    priority: 375\n")
        self.config = OrchestrationConfig.load(str(path))
        self.definition = describe_scheduler(self.config, SimpleNamespace(
            timezone_name="Asia/Shanghai", auction_enabled=True,
            auction_start_time="09:15", auction_execution_time="09:24:50",
            auction_submit_cutoff="09:25", execution_windows="14:30-14:55",
            scheduled_replan_times=("14:30",)))
        self.engine = TaskOrchestrator(stabilizer_factory=lambda _scope: ObservationStabilizer(self.engine))
        self.engine.register(TaskSpec("bootstrap", 1000, preemptible=False),
                             lambda: Process(done=True), bootstrap=True)
        self.processes = {name: Process() for name in self.config.tasks}
        for name, spec in self.config.tasks.items():
            self.engine.register(spec, lambda name=name: self.processes[name])
        self.now = int(datetime(2026, 11, 23, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()) * 10**9
        self.engine.start(self.now)

    def snapshot(self):
        return {**self.engine.snapshot(), "enabled": True, "configuration": deepcopy(self.definition),
                "execution": {"version": "v1", "complete": False, "pending_orders": 1,
                              "remaining": {"a": "100", "b": "0"}, "frozen": {"c": "limit"}}}

    def report(self):
        return build_scheduler_report(self.snapshot(), self.engine.actions(limit=1000))

    def test_cleanup_preserves_business_result_and_excludes_unadmitted_executions(self):
        task = self.processes["normal_execution"]
        task.done, task.clean = True, False
        self.engine.submit("normal_execution", Proposal("goal"))
        self.engine.tick(self.now + 1_123_456_789)
        self.engine.submit("manual_replan", Proposal("next"))
        report = self.report()
        self.assertEqual(report["current"][0]["state"], "stopping")
        self.assertEqual(report["current"][0]["final_result"]["reason"], "target_achieved")
        self.assertEqual(report["current"][0]["cleanup_result"]["reason"], "cancelling_existing_orders")
        self.assertEqual(report["current"][0]["started_at"], "2026-11-23T14:30:01.123+08:00")
        self.assertEqual(report["summary"]["executed"], 2)  # bootstrap and admitted execution
        self.assertEqual([r["task_id"] for r in report["queue"]], ["manual_replan"])
        self.assertNotIn("manual_replan", [r["task_id"] for r in report["history"]])
        self.assertEqual(report["execution"]["remaining_instruments"], 1)
        task.clean = True
        self.engine.tick(self.now + 2_000_000_000)
        finished = next(r for r in self.report()["history"] if r["task_id"] == "normal_execution")
        self.assertEqual(finished["reason"], "target_achieved")
        self.assertEqual(finished["finished_at"], "2026-11-23T14:30:02.000+08:00")

    def test_skipped_cleanup_and_policies_survive_read_only_projection(self):
        path = Path(self.directory.name) / "skip-tasks.yaml"
        path.write_text("schema_version: 2\ntasks:\n  normal_execution:\n"
                        "    skip_entry_cleanup: true\n    skip_exit_cleanup: true\n")
        config = OrchestrationConfig.load(str(path))
        self.definition = describe_scheduler(config, SimpleNamespace(
            timezone_name="Asia/Shanghai", auction_enabled=True,
            auction_start_time="09:24:01", auction_execution_time="09:24:58",
            auction_submit_cutoff="09:25:00", execution_windows="10:00-11:30,13:00-14:10",
            scheduled_replan_times=()))
        process = Process()
        # Use the real configured task ID in both the engine and its immutable view.
        engine = TaskOrchestrator(stabilizer_factory=lambda _: ObservationStabilizer(engine))
        engine.register(TaskSpec("bootstrap", 1000), lambda: Process(done=True), bootstrap=True)
        for name, spec in config.tasks.items():
            engine.register(spec, lambda name=name: process if name == "normal_execution" else Process())
        engine.start(self.now)
        self.engine = engine
        engine.submit("normal_execution", Proposal("normal"))
        engine.tick(self.now + 1)
        report = self.report()
        self.assertTrue(report["current"][0]["cleanup_result"]["evidence"]["cleanup_skipped"])
        task = next(t for t in report["tasks"] if t["task_id"] == "normal_execution")
        self.assertTrue(task["skip_entry_cleanup"])
        self.assertTrue(task["skip_exit_cleanup"])
        self.assertFalse(report["configuration"]["tasks"]["bootstrap"]["skip_entry_cleanup"])
        process.done = True
        engine.tick(self.now + 2)
        report = self.report()
        finished = next(r for r in report["history"] if r["task_id"] == "normal_execution")
        self.assertEqual(finished["final_result"]["state"], "succeeded")
        self.assertEqual(finished["cleanup_result"]["reason"], "cleanup_skipped_by_task_policy")
        self.assertEqual(finished["cleanup_result"]["evidence"]["kind"], "exit")
        self.assertEqual([a["evidence"]["kind"] for a in report["actions"]
                          if a["action"] == "CLEANUP_SKIPPED"], ["exit", "entry"])
        report["configuration"]["tasks"]["normal_execution"]["skip_entry_cleanup"] = False
        self.assertTrue(self.report()["configuration"]["tasks"]["normal_execution"]["skip_entry_cleanup"])

    def test_queue_priority_fifo_waiting_conditions_and_effective_overrides(self):
        for name in ("scheduled_replan", "price_limit_replan", "normal_execution", "manual_replan"):
            self.engine.submit(name, Proposal(name))
        report = self.report()
        self.assertEqual([r["task_id"] for r in report["queue"]],
                         ["manual_replan", "scheduled_replan", "price_limit_replan", "normal_execution"])
        self.assertNotIn("exposure_correction", [t["task_id"] for t in report["waiting"]])
        task = next(t for t in report["tasks"] if t["task_id"] == "exposure_correction")
        self.assertFalse(task["enabled"])
        self.assertEqual(task["priority"], 375)
        self.engine.set_enabled("exposure_correction", True)
        updated = next(t for t in self.report()["tasks"] if t["task_id"] == "exposure_correction")
        self.assertTrue(updated["enabled"])
        self.assertFalse(report["configuration"]["tasks"]["exposure_correction"]["enabled"])
        self.assertEqual(updated["trigger"]["kind"], "condition")

    def test_racing_action_revision_and_missing_old_timestamps(self):
        state = self.snapshot()
        self.engine.submit("manual_replan", Proposal("after-snapshot"))
        report = build_scheduler_report(state, self.engine.actions(limit=1000))
        self.assertTrue(all(a["sequence"] <= state["revision"] for a in report["actions"]))
        self.assertNotIn("manual_replan", [r["task_id"] for r in report["queue"]])
        old = build_scheduler_report(state, [])
        self.assertNotIn("started_at", old["history"][0])
        self.assertEqual(old["history"][0]["state"], "succeeded")

    def test_history_uses_actual_execution_order_instead_of_enqueue_order(self):
        self.processes["normal_execution"].done = True
        self.processes["manual_replan"].done = True
        self.engine.submit("normal_execution", Proposal("queued-first"))
        self.engine.submit("manual_replan", Proposal("higher-priority"))
        self.engine.tick(self.now + 1)
        self.engine.tick(self.now + 2)
        self.assertEqual([r["task_id"] for r in self.report()["history"]],
                         ["normal_execution", "manual_replan", "bootstrap"])

    def test_stop_and_pause_are_observed_without_generating_new_work(self):
        self.engine.paused = True
        paused = self.report()
        self.assertTrue(paused["paused"])
        revision = self.engine.snapshot()["revision"]
        self.report()
        self.assertEqual(self.engine.snapshot()["revision"], revision)
        self.engine.stop()
        self.assertEqual(self.report()["lifecycle"], "stopping")
        self.engine.tick(self.now + 1)
        self.assertEqual(self.report()["lifecycle"], "stopped")

    def test_dashboard_proxy_reads_only_and_handles_disabled_missing_unreachable_nodes(self):
        state, actions = self.snapshot(), self.engine.actions(limit=1000)

        class Node:
            def __init__(self):
                self.calls = []
                self.state = state

            def get(self, path, params=None):
                self.calls.append((path, params))
                if path == "/scheduler/state":
                    return self.state
                if path == "/scheduler/actions":
                    return {"actions": actions}
                raise AssertionError(path)

        node = Node()
        data = SimpleNamespace(node_api=lambda account, trader: node)
        result = scheduler_overview("a", "t", history_limit=1, data=data)
        self.assertEqual(result["history_total"], 1)
        self.assertEqual(node.calls, [("/scheduler/state", None), ("/scheduler/actions", {
            "after_sequence": 0, "limit": 1000})])
        node.calls.clear()
        node.state = {"enabled": False}
        self.assertEqual(scheduler_overview("a", "t", history_limit=100, data=data), {"enabled": False})
        self.assertEqual(node.calls, [("/scheduler/state", None)])
        data.node_api = lambda account, trader: None
        with self.assertRaises(HTTPException) as missing:
            scheduler_overview("a", "t", data=data)
        self.assertEqual(missing.exception.status_code, 404)
        data.node_api = lambda account, trader: node
        with patch.object(node, "get", side_effect=RuntimeError("node unavailable")):
            with self.assertRaises(HTTPException) as offline:
                scheduler_overview("a", "t", data=data)
        self.assertEqual(offline.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
