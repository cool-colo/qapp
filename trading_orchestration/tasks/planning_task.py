from task_orchestration.models import Decision, RunState, TaskContext, TERMINAL
from task_orchestration.components.processes.task_process import BusinessTask
from task_orchestration.components.planners.goal_planner import InterruptibleGoalPlanner


class PlanningTask(BusinessTask):
    """Task policy and lifecycle; goal preparation belongs to the injected planner."""

    def __init__(self, bridge, planner: InterruptibleGoalPlanner[TaskContext, Decision],
                 *, source='automatic', scheduled=False):
        self.bridge, self.planner, self.source = bridge, planner, source
        self.scheduled = scheduled
        self.done = False

    def advance(self, context):
        host = self.bridge.host
        if self.source != 'manual' and (host._trading_paused or host._target_updates_paused):
            return Decision(RunState.WAITING, 'user_paused')
        if self.source == 'automatic' and not host.automatic_replanning_allowed():
            return Decision(RunState.CANCELLED, 'automatic_planning_protected')
        if self.scheduled and (host._clock_date() not in host._trading_dates
                               or host._target_date != host._clock_date() or not host._target_version):
            return Decision(RunState.CANCELLED, 'scheduled_planning_not_applicable')
        validity = context.proposal.payload
        if validity is not None and not validity.allows(host, host._clock_date()):
            return Decision(RunState.EXPIRED, 'planning_window_closed')
        result = self.planner.plan(context)
        self.done = result.state in TERMINAL
        return result

    def interrupt(self, context):
        if not self.done:
            self.planner.abort()
