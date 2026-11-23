from task_orchestration.models import Decision, RunState


class InitialGoalPlanner:
    def __init__(self, bridge, delegate):
        self.bridge, self.delegate = bridge, delegate

    def plan(self, context):
        host = self.bridge.host
        day = host._clock_date()
        if host._target_date == day and host._target_version:
            result = Decision(RunState.SUCCEEDED, 'reuse_current_target')
        else:
            result = self.delegate.plan(context)
        if result.state == RunState.SUCCEEDED:
            host._processed_dates.add(day)
        return result

    def abort(self, reason='planning_interrupted'):
        self.delegate.abort(reason)
