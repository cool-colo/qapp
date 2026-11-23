from task_orchestration.models import Decision, RunState
from task_orchestration.components.processes.task_process import BusinessTask


class ManualExecutionTask(BusinessTask):
    """Keep the existing manual trigger's pause bypass and execution window."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.installed = False

    def advance(self, context):
        if not self.bridge.host._within_execution_window():
            return Decision(RunState.WAITING, "manual_execution_window_closed")
        target = context.proposal.payload
        if not self.installed:
            if self.bridge.host._clock_date() != target.trading_date:
                return Decision(RunState.EXPIRED, "manual_target_date_expired")
            self.bridge.install_quantities(dict(target.quantities), target.version, target.reason)
            self.installed = True
        self.bridge.execute(trigger=target.trigger)
        return Decision(RunState.SUCCEEDED if self.bridge.target_satisfied() else RunState.RUNNING,
                        "manual_target_evaluated")
