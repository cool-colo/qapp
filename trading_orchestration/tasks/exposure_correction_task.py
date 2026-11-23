from __future__ import annotations

from task_orchestration.components.processes.task_process import BusinessTask
from task_orchestration.models import Decision, RunState
from task_orchestration.components.assessors.progress_assessor import ProgressAssessor
from task_orchestration.components.planners.goal_planner import GoalPlanner


class ExposureCorrectionTask(BusinessTask):
    def __init__(self, bridge, *, assessor: ProgressAssessor, planner: GoalPlanner,
                 executor_factory, risk_planner_factory, outcome_policy):
        self.bridge = bridge
        self.assessor, self.planner = assessor, planner
        self.executor_factory = executor_factory
        self.risk_planner_factory = risk_planner_factory
        self.outcome_policy = outcome_policy
        self.executor = executor_factory()
        self.active_plan = None
        self.risk_planner = None

    @staticmethod
    def _fingerprint(snapshot):
        return (snapshot.target.version if snapshot.target else None,
                tuple((iid, str(item.quantity), str(item.sellable), item.can_buy, item.can_sell)
                      for iid, item in sorted(snapshot.instruments.items())))

    def advance(self, context):
        bridge, host = self.bridge, self.bridge.host
        if not bridge.in_windows(bridge.config.exposure.windows):
            return Decision(RunState.EXPIRED, "correction_window_closed")
        if not host.automatic_replanning_allowed():
            return Decision(RunState.WAITING, "automatic_execution_paused")
        snapshot = bridge.snapshot_provider.capture()
        assessment = self.assessor.assess(snapshot)
        if assessment.status == "unknown":
            return Decision(RunState.WAITING, assessment.reason, assessment.evidence())

        if self.risk_planner is not None:
            result = self.risk_planner.plan(context)
            if result.state == RunState.WAITING:
                return result
            bridge.exposure_block = self._fingerprint(bridge.snapshot_provider.capture())
            self.risk_planner = None
            return Decision(RunState.WAITING, "reassess_after_risk_manager",
                            {"planning_result": result.reason})

        if self.active_plan is not None:
            same_direction = (assessment.status == "correction"
                              and assessment.direction == self.active_plan.direction
                              and snapshot.target.version == self.active_plan.parent_version)
            if snapshot.pending_orders and same_direction:
                with bridge.operations(windows=bridge.config.exposure.windows):
                    host._reconcile_unfilled_orders(host._clock_date())
                return Decision(RunState.WAITING, "awaiting_corrective_orders", assessment.evidence())
            # The framework stops execution and drains orders before the next
            # business pass. Completion must use the post-cleanup portfolio.
            return Decision(RunState.WAITING, "correction_requires_fresh_state",
                            assessment.evidence(), requires_stable=True)

        if snapshot.pending_orders:
            return Decision(RunState.WAITING, "unexpected_pending_orders", requires_stable=True)
        if assessment.status == "satisfied":
            bridge.exposure_block = None
            return Decision(RunState.SUCCEEDED, "exposure_within_tolerance", assessment.evidence())
        plan = self.planner.plan(snapshot)
        action = self.outcome_policy.decide(plan)
        if action == "wait":
            return Decision(RunState.WAITING, plan.reason)
        if action == "complete":
            return Decision(RunState.SUCCEEDED, plan.reason, assessment.evidence())
        if action == "request_risk_manager":
            fingerprint = self._fingerprint(snapshot)
            if bridge.exposure_block == fingerprint:
                return Decision(RunState.WAITING, "unchanged_insufficient_capacity",
                                {**assessment.evidence(), "capacity": str(plan.available_notional),
                                 "required": str(plan.required_notional), "excluded": dict(plan.excluded)})
            bridge.exposure_block = fingerprint
            bridge.restore_formal()
            self.risk_planner = self.risk_planner_factory()
            return Decision(RunState.WAITING, "request_risk_manager_capacity_insufficient",
                            {"capacity": str(plan.available_notional), "required": str(plan.required_notional),
                             "excluded": dict(plan.excluded)})
        if action != "execute":
            raise ValueError(f"unsupported exposure outcome: {action}")
        applied = self.executor.apply(context, plan)
        if applied.state == RunState.FAILED:
            return Decision(RunState.WAITING, applied.reason)
        self.active_plan = plan
        self.executor.start(context)
        return Decision(RunState.RUNNING, "single_side_correction",
                        {**assessment.evidence(), "planned_notional": str(plan.notional),
                         "quantities": {k: str(v) for k, v in plan.quantities.items()}})

    def interrupt(self, context):
        if self.risk_planner is not None:
            self.risk_planner.abort()

    def after_stable(self, context):
        self.executor.release(context)
        self.active_plan = None
        self.risk_planner = None
        self.executor = self.executor_factory()

