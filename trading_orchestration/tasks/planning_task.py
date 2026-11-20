from task_orchestration.models import Decision, RunState
from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer
from trading_orchestration.components.planners.risk_manager_goal_planner import RiskManagerGoalPlanner


class PlanningTask:
    """Shared planning workflow; each task definition binds its own policies."""

    def __init__(self, bridge, *, source="automatic", auction=False):
        self.bridge, self.source, self.auction = bridge, source, auction
        self.stabilizer = NautilusStateStabilizer(bridge)
        self.planner = None
        self.done = False

    def advance(self, context):
        host = self.bridge.host
        if self.source != "manual" and (host._trading_paused or host._target_updates_paused):
            return Decision(RunState.WAITING, "user_paused")
        if self.source == "automatic" and not host.automatic_replanning_allowed():
            return Decision(RunState.CANCELLED, "automatic_planning_protected")
        validity = context.proposal.payload
        if validity is not None and not validity.allows(host, host._clock_date()):
            return Decision(RunState.EXPIRED, "planning_window_closed")
        stable = self.stabilizer.stabilize(context)
        if stable.state != RunState.SUCCEEDED:
            return stable
        day = host._clock_date()
        if self.source == "initial" and host._target_date == day and host._target_version:
            result = Decision(RunState.SUCCEEDED, "reuse_current_target")
        else:
            if self.planner is None:
                snapshot = host._auction_planning_contexts.get(day) if self.auction else None
                if self.auction and snapshot is None:
                    return Decision(RunState.WAITING, "missing_auction_snapshot")
                self.planner = RiskManagerGoalPlanner(self.bridge, source=self.source, planning_context=snapshot)
            result = self.planner.plan(context)
        if result.state == RunState.SUCCEEDED:
            if self.source == "initial":
                host._processed_dates.add(day)
            if self.auction:
                host._auction_frozen_dates.add(day)
                signal = host._resolve_signal_date(day)
                host._auction_prediction_ranks = {
                    str(host._instrument_by_stock[code]): rank
                    for code, rank in host._prediction_ranks_by_date.get(signal, {}).items()
                    if code in host._instrument_by_stock}
            self.done = True
        return result

    def quiesce(self, context):
        if self.planner is not None and not self.done:
            self.planner.abort()
        return self.stabilizer.stabilize(context)

