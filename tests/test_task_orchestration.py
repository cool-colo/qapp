from dataclasses import replace
from copy import deepcopy
from decimal import Decimal as D
import unittest

from task_orchestration.core.orchestrator import TaskOrchestrator
from task_orchestration.models import Decision, Proposal, RunState, TaskSpec
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.components.planners.remaining_target_exposure_planner import RemainingTargetExposurePlanner
from trading_orchestration.configuration.exposure_config import ExposureConfig
from trading_orchestration.contexts.exposure_context import (
    FormalTarget, ExposureContext, InstrumentSnapshot, PendingExposureOrder,
)
from trading_orchestration.components.allocations.exposure_order_admission import ExposureOrderAdmission


class Process:
    def __init__(self, *, done=False):
        self.done = done
        self.clean = True
        self.visits = []
        self.cleanup_error = None
        self.cleanup_evidence = {}
        self.cleanup_reason = "cleanup"
    def advance(self, ctx):
        self.visits.append(ctx)
        return Decision(RunState.SUCCEEDED if self.done else RunState.RUNNING, "work")
    def interrupt(self, ctx):
        pass
    def after_stable(self, ctx):
        pass


class OutcomeProcess(Process):
    def __init__(self, outcome, *, clean=False):
        super().__init__()
        self.outcome = outcome
        self.clean = clean
        self.cleanup_evidence = {"orders": ["order-1"]}
        self.cleanup_error = None

    def advance(self, ctx):
        self.visits.append(ctx)
        return self.outcome



class ControlledStabilizer:
    """Fake external state, independent of task lifecycle methods."""
    def __init__(self, engine):
        self.engine = engine

    def stabilize(self, ctx):
        process = self.engine._runs[ctx.run_id].process
        if process is None:
            return Decision(RunState.SUCCEEDED, "entry_clean")
        if process.cleanup_error is not None:
            raise process.cleanup_error
        reason = ("execution_stable" if process.clean else "awaiting_terminal_orders"
                  ) if isinstance(process, OutcomeProcess) else process.cleanup_reason
        return Decision(RunState.SUCCEEDED if process.clean else RunState.WAITING,
                        reason, process.cleanup_evidence)


def test_engine():
    engine = TaskOrchestrator(stabilizer_factory=lambda _scope: ControlledStabilizer(engine))
    return engine


test_engine.__test__ = False


class OrchestratorTest(unittest.TestCase):
    def engine(self):
        engine = test_engine()
        engine.register(TaskSpec("boot", 0), lambda: Process(done=True), bootstrap=True)
        engine.start(0)
        return engine

    def test_event_wake_advances_tasks_without_evaluating_condition_triggers(self):
        from unittest.mock import Mock
        engine = self.engine()
        trigger = Mock()
        trigger.evaluate.return_value = None
        process = Process()
        engine.register(TaskSpec("timed", 1), lambda: process)
        engine.register(TaskSpec("condition", 2), Process, trigger)
        engine.submit("timed", Proposal("timer"))
        engine.tick(1, evaluate_triggers=False)
        self.assertTrue(process.visits)
        trigger.evaluate.assert_not_called()
        engine.tick(2)
        trigger.evaluate.assert_called_once_with(2)

    def test_high_priority_waits_for_confirmed_cleanup_and_revokes_old_epoch(self):
        engine = self.engine()
        low, high = Process(), Process()
        low.clean = False
        engine.register(TaskSpec("low", 1), lambda: low)
        engine.register(TaskSpec("high", 2), lambda: high)
        engine.submit("low", Proposal("a"))
        engine.tick(1)
        old = low.visits[-1]
        engine.submit("high", Proposal("b"))
        engine.tick(2)
        self.assertEqual(engine.owner_task(), "low")
        self.assertFalse(high.visits)
        low.clean = True
        engine.tick(3)
        self.assertEqual(engine.owner_task(), "high")
        self.assertFalse(engine.owns(old))
        self.assertTrue(engine.owns(high.visits[-1]))

    def test_same_priority_fifo_and_disable_does_not_drop_owner(self):
        engine = self.engine()
        engine.register(TaskSpec("a", 1), Process)
        engine.register(TaskSpec("b", 1), Process)
        engine.submit("a", Proposal("a"))
        engine.submit("b", Proposal("b"))
        engine.tick(1)
        engine.set_enabled("a", False)
        engine.tick(2)
        self.assertEqual(engine.owner_task(), "a")
        engine.set_enabled("b", False)
        self.assertEqual(engine.snapshot()["runs"][-1]["state"], "cancelled")

    def test_failed_cleanup_keeps_ownership(self):
        engine = self.engine()
        process = Process(done=True)
        process.clean = False
        engine.register(TaskSpec("a", 1), lambda: process)
        engine.submit("a", Proposal("a"))
        engine.tick(1)
        self.assertEqual(engine.owner_task(), "a")
        self.assertEqual(engine.snapshot()["runs"][-1]["state"], "stopping")
        process.clean = True
        engine.tick(2)
        self.assertIsNone(engine.owner_task())

    def test_business_outcome_survives_immediate_and_delayed_cleanup(self):
        for state in (RunState.SUCCEEDED, RunState.FAILED, RunState.UNACHIEVABLE):
            for clean in (True, False):
                with self.subTest(state=state, immediate_cleanup=clean):
                    engine = self.engine()
                    evidence = {"target_version": "v1", "assessment": {"deviation": "0.01"}}
                    expected = deepcopy(evidence)
                    process = OutcomeProcess(Decision(state, "business_result", evidence), clean=clean)
                    engine.register(TaskSpec("a", 1), lambda: process)
                    run_id = engine.submit("a", Proposal("a"))
                    engine.tick(1)
                    if not clean:
                        pending = engine.snapshot()["runs"][-1]
                        self.assertEqual(pending["state"], "stopping")
                        self.assertEqual(pending["final_result"], {
                            "state": state.value, "reason": "business_result", "evidence": expected})
                        self.assertEqual(engine.owner_task(), "a")
                        self.assertFalse([r for r in engine.records
                                          if r.run_id == run_id and r.action == "TERMINATED"])
                        # A component and a caller must not mutate retained context.
                        evidence["assessment"]["deviation"] = "99"
                        pending["final_result"]["evidence"]["assessment"]["deviation"] = "88"
                        process.cleanup_evidence["orders"].clear()
                        process.clean = True
                        engine.tick(2)
                    terminal = [r for r in engine.records
                                if r.run_id == run_id and r.action == "TERMINATED"]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0].reason, "business_result")
                    self.assertEqual(terminal[0].evidence, {**expected, "state": state.value})
                    result = engine.snapshot()["runs"][-1]
                    self.assertEqual(result["reason"], "business_result")
                    self.assertEqual(result["final_result"]["evidence"], expected)
                    self.assertEqual(result["cleanup_result"]["reason"], "execution_stable")
                    self.assertIsNone(engine.owner_task())
                    if not clean:
                        waiting = [r for r in engine.actions() if r["run_id"] == run_id
                                   and r["action"] == "WAITING_CLEANUP"]
                        self.assertEqual(waiting[0]["evidence"]["orders"], ["order-1"])
                        waiting[0]["evidence"]["orders"].clear()
                        self.assertEqual(next(r for r in engine.actions()
                                             if r["run_id"] == run_id and r["action"] == "WAITING_CLEANUP")
                                         ["evidence"]["orders"], ["order-1"])

    def test_cleanup_error_does_not_replace_outcome_or_release_owner(self):
        engine = self.engine()
        process = OutcomeProcess(Decision(RunState.SUCCEEDED, "target_achieved", {"remaining": 0}))
        process.cleanup_error = RuntimeError("reconciliation unavailable")
        engine.register(TaskSpec("a", 1), lambda: process)
        engine.register(TaskSpec("next", 2), Process)
        run_id = engine.submit("a", Proposal("a"))
        engine.tick(1)
        engine.submit("next", Proposal("next"))
        engine.tick(2)
        self.assertEqual(engine.owner_task(), "a")
        process.cleanup_error = None
        process.clean = True
        engine.tick(3)
        self.assertEqual(engine.owner_task(), "next")
        terminal = next(r for r in engine.records if r.run_id == run_id and r.action == "TERMINATED")
        self.assertEqual((terminal.reason, terminal.evidence),
                         ("target_achieved", {"state": "succeeded", "remaining": 0}))

    def test_control_termination_retains_its_cause_through_cleanup(self):
        for cause, state, reason in (("expiry", "expired", "proposal_expired"),
                                     ("cancel", "cancelled", "cancelled"),
                                     ("stop", "cancelled", "shutdown")):
            with self.subTest(cause=cause):
                engine = self.engine()
                process = Process()
                process.clean = False
                engine.register(TaskSpec("a", 1), lambda: process)
                run_id = engine.submit("a", Proposal("a", expires_ns=2 if cause == "expiry" else None))
                engine.tick(1)
                if cause == "cancel":
                    engine.cancel(run_id)
                elif cause == "stop":
                    engine.stop()
                engine.tick(2)
                self.assertEqual(engine.owner_task(), "a")
                process.clean = True
                engine.tick(3)
                terminal = next(r for r in engine.records
                                if r.run_id == run_id and r.action == "TERMINATED")
                self.assertEqual((terminal.reason, terminal.evidence["state"]), (reason, state))

    def test_late_expiry_cancel_or_shutdown_does_not_rewrite_business_result(self):
        for cause in ("expiry", "cancel", "stop"):
            with self.subTest(cause=cause):
                engine = self.engine()
                process = OutcomeProcess(Decision(RunState.UNACHIEVABLE, "capacity_insufficient",
                                                  {"required": "1000", "capacity": "100"}))
                engine.register(TaskSpec("a", 1), lambda: process)
                run_id = engine.submit("a", Proposal("a", expires_ns=2))
                engine.tick(1)
                if cause == "cancel":
                    engine.cancel(run_id)
                elif cause == "stop":
                    engine.stop()
                engine.tick(2)
                self.assertEqual(engine.owner_task(), "a")
                process.clean = True
                engine.tick(3)
                terminal = next(r for r in engine.records
                                if r.run_id == run_id and r.action == "TERMINATED")
                self.assertEqual(terminal.reason, "capacity_insufficient")
                self.assertEqual(terminal.evidence, {
                    "state": "unachievable", "required": "1000", "capacity": "100"})

    def test_restart_is_empty_not_checkpoint_recovery(self):
        engine = self.engine()
        engine.register(TaskSpec("a", 1), Process)
        engine.submit("a", Proposal("old"))
        fresh = self.engine()
        self.assertEqual(len(fresh.snapshot()["runs"]), 1)
        self.assertEqual(fresh.snapshot()["lifecycle"], "ready")

    def test_bootstrap_prevents_higher_priority_normal_task(self):
        engine = test_engine()
        boot = Process()
        engine.register(TaskSpec("boot", 0, preemptible=False), lambda: boot, bootstrap=True)
        engine.register(TaskSpec("ordinary", 1000), Process)
        engine.start(0)
        engine.submit("ordinary", Proposal("x"))
        engine.tick(1)
        self.assertEqual(engine.owner_task(), "boot")
        boot.done = True
        engine.tick(2)
        engine.tick(3)
        self.assertEqual(engine.owner_task(), "ordinary")

    def test_bootstrap_delayed_cleanup_preserves_shutdown_and_success_result(self):
        for stop_requested in (True, False):
            with self.subTest(stop_requested=stop_requested):
                engine = test_engine()
                evidence = {"target_version": "v1", "restored": {"positions": 3}}
                boot = OutcomeProcess(Decision(RunState.SUCCEEDED,
                                               "initialized_from_business_facts", evidence))
                ordinary = Process()
                engine.register(TaskSpec("boot", 0, preemptible=False), lambda: boot, bootstrap=True)
                engine.register(TaskSpec("ordinary", 1000), lambda: ordinary)
                engine.start(0)
                engine.submit("ordinary", Proposal("queued_during_bootstrap"))
                self.assertEqual(engine.lifecycle, "initializing")

                if stop_requested:
                    engine.stop()
                    self.assertIsNone(engine.submit("ordinary", Proposal("during_shutdown")))
                engine.tick(1)
                self.assertEqual(engine.lifecycle, "stopping" if stop_requested else "initializing")
                self.assertEqual(engine.owner_task(), "boot")
                self.assertFalse(ordinary.visits)

                boot.clean = True
                engine.tick(2)
                self.assertEqual(engine.lifecycle, "stopped" if stop_requested else "ready")
                self.assertEqual(engine.owner_task(), None if stop_requested else "ordinary")
                result = engine.snapshot()["runs"][0]
                self.assertEqual(result["state"], "succeeded")
                self.assertEqual(result["final_result"], {
                    "state": "succeeded", "reason": "initialized_from_business_facts",
                    "evidence": evidence})
                terminal = [r for r in engine.records if r.task_id == "boot" and r.action == "TERMINATED"]
                self.assertEqual(len(terminal), 1)
                self.assertEqual(terminal[0].reason, "initialized_from_business_facts")
                self.assertEqual(terminal[0].evidence, {**evidence, "state": "succeeded"})

                engine.tick(3)
                if stop_requested:
                    self.assertEqual(engine.lifecycle, "stopped")
                    self.assertFalse(ordinary.visits)
                    self.assertIsNone(engine.submit("ordinary", Proposal("after_shutdown")))
                else:
                    self.assertEqual(engine.owner_task(), "ordinary")
                    self.assertTrue(ordinary.visits)

    def test_repeated_runs_are_deterministic(self):
        def run():
            engine = self.engine()
            engine.register(TaskSpec("a", 1), lambda: Process(done=True))
            engine.submit("a", Proposal("x"))
            engine.tick(5)
            return engine.snapshot(), engine.records
        self.assertEqual(run(), run())

    def test_pause_and_cancel_retain_owner_until_cleanup(self):
        engine = self.engine()
        process = Process()
        process.clean = False
        engine.register(TaskSpec("a", 1), lambda: process)
        run_id = engine.submit("a", Proposal("a"))
        engine.tick(1)
        engine.paused = True
        engine.cancel(run_id)
        engine.tick(2)
        self.assertEqual(engine.owner_task(), "a")
        process.clean = True
        engine.tick(3)
        self.assertIsNone(engine.owner_task())
        self.assertEqual(engine.snapshot()["runs"][-1]["state"], "cancelled")

    def test_expired_waiting_task_never_starts(self):
        engine = self.engine()
        process = Process()
        engine.register(TaskSpec("a", 1), lambda: process)
        engine.submit("a", Proposal("a", expires_ns=2))
        engine.tick(2)
        self.assertFalse(process.visits)
        self.assertEqual(engine.snapshot()["runs"][-1]["state"], "expired")

    def test_reentrant_tick_is_serialized(self):
        engine = self.engine()
        class ReentrantProcess(Process):
            def advance(self, ctx):
                if ctx.now_ns == 1:
                    engine.tick(2)
                return super().advance(ctx)
        process = ReentrantProcess()
        engine.register(TaskSpec("a", 1), lambda: process)
        engine.submit("a", Proposal("a"))
        engine.tick(1)
        self.assertEqual([c.now_ns for c in process.visits], [1, 2])


def snapshot(quantity="9000", target_qty="8000", weight="0.8", *, assets="100000",
             buffer="0", sellable=None, pending=0):
    quantity, target_qty = D(quantity), D(target_qty)
    target = FormalTarget("v1", "2026-07-08", {"A": target_qty}, {"A": D(weight)},
                          D(buffer), D(assets) - D(buffer))
    item = InstrumentSnapshot(quantity, D(10), quantity if sellable is None else D(sellable))
    return ExposureContext(0, "2026-07-08", 1, D(assets), D(assets) - quantity * 10,
                           {"A": item}, target, pending)


class ExposurePolicyTest(unittest.TestCase):
    def setUp(self):
        self.assessor = ExposureAssessor()
        self.planner = RemainingTargetExposurePlanner(ExposureConfig())

    def test_five_percentage_points_is_inclusive(self):
        self.assertEqual(self.assessor.assess(snapshot("8500").position_snapshot()).status, "satisfied")
        self.assertEqual(self.assessor.assess(snapshot("7500").position_snapshot()).status, "satisfied")
        self.assertEqual(self.assessor.assess(snapshot("8501").position_snapshot()).direction, "sell")
        self.assertEqual(self.assessor.assess(snapshot("7499").position_snapshot()).direction, "buy")

    def test_fixed_buffer_is_not_recomputed_percentage(self):
        first = snapshot("8000", assets="100000", buffer="12000")
        changed = replace(first, total_assets=D(102000))
        self.assertEqual(self.assessor.assess(changed.position_snapshot()).denominator, D(90000))
        self.assertEqual(changed.target.buffer_amount, D(12000))

    def test_sells_to_center(self):
        result = self.planner.plan(snapshot())
        self.assertEqual(result.status, "planned")
        self.assertEqual(result.quantities["A"], D(8000))
        self.assertEqual(result.notional, D(10000))

    def test_buys_to_center(self):
        result = self.planner.plan(snapshot("7000"))
        self.assertEqual(result.status, "planned")
        self.assertEqual(result.quantities["A"], D(8000))

    def test_capacity_insufficient_even_with_tradable_stocks(self):
        result = self.planner.plan(snapshot(target_qty="8900"))
        self.assertEqual(result.status, "replan")
        self.assertEqual(result.available_notional, D(1000))
        self.assertEqual(result.required_notional, D(5000))

    def test_all_blocked_requests_new_formal_plan(self):
        ctx = snapshot()
        ctx = replace(ctx, instruments={"A": replace(ctx.instruments["A"], can_sell=False)})
        self.assertEqual(self.planner.plan(ctx).status, "replan")

    def test_can_use_other_remaining_target_when_one_is_blocked(self):
        ctx = snapshot(quantity="4500", target_qty="4000", weight="0.4")
        target = replace(ctx.target, quantities={"A": D(4000), "B": D(4000)},
                         weights={"A": D(".4"), "B": D(".4")})
        ctx = replace(ctx, target=target, free_cash=D(10000), instruments={
            "A": replace(ctx.instruments["A"], can_sell=False),
            "B": replace(ctx.instruments["A"], quantity=D(4500))})
        plan = self.planner.plan(ctx)
        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.quantities["A"], D(4500))
        self.assertEqual(plan.quantities["B"], D(4000))

    def test_pending_orders_prevent_duplicate_plan_and_completion(self):
        self.assertEqual(self.planner.plan(snapshot(pending=1)).status, "waiting")
        self.assertEqual(self.assessor.assess(snapshot("8500", pending=1).position_snapshot()).status, "settling")

    def test_missing_data_is_unknown_not_infeasible(self):
        ctx = replace(snapshot(), quality_error="stale_price")
        self.assertEqual(self.planner.plan(ctx).status, "unknown")
        self.assertEqual(self.assessor.assess(replace(snapshot(), total_assets=D(0)).position_snapshot()).status, "unknown")

    def test_sellable_constraint_and_fee_free_buy_cash_are_applied(self):
        self.assertEqual(self.planner.plan(snapshot(sellable="100")).status, "replan")
        ctx = replace(snapshot("7000"), free_cash=D(5000))
        self.assertEqual(self.planner.plan(ctx).quantities["A"], D(7500))

    def test_context_mappings_are_immutable(self):
        ctx = snapshot()
        with self.assertRaises(TypeError):
            ctx.target.quantities["A"] = D(1)
        with self.assertRaises(TypeError):
            ctx.instruments["A"] = ctx.instruments["A"]

    def test_physical_orders_include_pending_commitments(self):
        ctx = snapshot("7000")
        admission = ExposureOrderAdmission()
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000), order_price=D(10)), D(1000))
        ctx = replace(ctx, pending_orders=1, pending=(PendingExposureOrder("A", "buy", D(300)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000), order_price=D(10)), D(700))
        ctx = replace(ctx, pending=(PendingExposureOrder("A", "buy", D(500)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000), order_price=D(10)), D(500))

    def test_physical_order_blocks_opposite_pending_and_boundary(self):
        admission = ExposureOrderAdmission()
        ctx = replace(snapshot("7000"), pending_orders=1,
                      pending=(PendingExposureOrder("A", "sell", D(100)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(500), order_price=D(10)), D(0))
        self.assertEqual(admission.quantity(snapshot("7500"), "A", "buy", D(100), order_price=D(10)), D(100))

    def test_order_never_exceeds_formal_remaining_quantity(self):
        ctx = snapshot("7000", target_qty="7100")
        self.assertEqual(ExposureOrderAdmission().quantity(ctx, "A", "buy", D(500), order_price=D(10)), D(100))

