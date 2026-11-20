from dataclasses import replace
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
    def advance(self, ctx):
        self.visits.append(ctx)
        return Decision(RunState.SUCCEEDED if self.done else RunState.RUNNING, "work")
    def quiesce(self, ctx):
        return Decision(RunState.SUCCEEDED if self.clean else RunState.WAITING, "cleanup")


class OrchestratorTest(unittest.TestCase):
    def engine(self):
        engine = TaskOrchestrator()
        engine.register(TaskSpec("boot", 0), lambda: Process(done=True), bootstrap=True)
        engine.start(0)
        return engine

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

    def test_restart_is_empty_not_checkpoint_recovery(self):
        engine = self.engine()
        engine.register(TaskSpec("a", 1), Process)
        engine.submit("a", Proposal("old"))
        fresh = self.engine()
        self.assertEqual(len(fresh.snapshot()["runs"]), 1)
        self.assertEqual(fresh.snapshot()["lifecycle"], "ready")

    def test_bootstrap_prevents_higher_priority_normal_task(self):
        engine = TaskOrchestrator()
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
    item = InstrumentSnapshot(quantity, D(10), quantity if sellable is None else D(sellable),
                              buy_price=D(10), sell_price=D(10))
    return ExposureContext(0, "2026-07-08", 1, D(assets), D(assets) - quantity * 10,
                           {"A": item}, target, pending)


class ExposurePolicyTest(unittest.TestCase):
    def setUp(self):
        self.assessor = ExposureAssessor()
        self.planner = RemainingTargetExposurePlanner(ExposureConfig())

    def test_five_percentage_points_is_inclusive(self):
        self.assertEqual(self.assessor.assess(snapshot("8500")).status, "satisfied")
        self.assertEqual(self.assessor.assess(snapshot("7500")).status, "satisfied")
        self.assertEqual(self.assessor.assess(snapshot("8501")).direction, "sell")
        self.assertEqual(self.assessor.assess(snapshot("7499")).direction, "buy")

    def test_fixed_buffer_is_not_recomputed_percentage(self):
        first = snapshot("8000", assets="100000", buffer="12000")
        changed = replace(first, total_assets=D(102000))
        self.assertEqual(self.assessor.assess(changed).denominator, D(90000))
        self.assertEqual(changed.target.buffer_amount, D(12000))

    def test_sells_only_to_nearest_boundary(self):
        result = self.planner.plan(snapshot())
        self.assertEqual(result.status, "planned")
        self.assertEqual(result.quantities["A"], D(8500))
        self.assertEqual(result.notional, D(5000))

    def test_buys_only_to_nearest_boundary(self):
        result = self.planner.plan(snapshot("7000"))
        self.assertEqual(result.status, "planned")
        self.assertEqual(result.quantities["A"], D(7500))

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
        self.assertEqual(self.assessor.assess(snapshot("8500", pending=1)).status, "settling")

    def test_missing_data_is_unknown_not_infeasible(self):
        ctx = replace(snapshot(), quality_error="stale_price")
        self.assertEqual(self.planner.plan(ctx).status, "unknown")
        self.assertEqual(self.assessor.assess(replace(snapshot(), total_assets=D(0))).status, "unknown")

    def test_sellable_constraint_and_buy_fees_are_applied(self):
        self.assertEqual(self.planner.plan(snapshot(sellable="100")).status, "replan")
        ctx = replace(snapshot("7000"), free_cash=D(5000))
        self.assertEqual(self.planner.plan(ctx).status, "replan")

    def test_context_mappings_are_immutable(self):
        ctx = snapshot()
        with self.assertRaises(TypeError):
            ctx.target.quantities["A"] = D(1)
        with self.assertRaises(TypeError):
            ctx.instruments["A"] = ctx.instruments["A"]

    def test_physical_orders_include_pending_commitments(self):
        ctx = snapshot("7000")
        admission = ExposureOrderAdmission()
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000)), D(500))
        ctx = replace(ctx, pending_orders=1, pending=(PendingExposureOrder("A", "buy", D(300)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000)), D(200))
        ctx = replace(ctx, pending=(PendingExposureOrder("A", "buy", D(500)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(1000)), D(0))

    def test_physical_order_blocks_opposite_pending_and_boundary(self):
        admission = ExposureOrderAdmission()
        ctx = replace(snapshot("7000"), pending_orders=1,
                      pending=(PendingExposureOrder("A", "sell", D(100)),))
        self.assertEqual(admission.quantity(ctx, "A", "buy", D(500)), D(0))
        self.assertEqual(admission.quantity(snapshot("7500"), "A", "buy", D(100)), D(0))

    def test_order_never_exceeds_formal_remaining_quantity(self):
        ctx = snapshot("7000", target_qty="7100")
        self.assertEqual(ExposureOrderAdmission().quantity(ctx, "A", "buy", D(500)), D(100))

