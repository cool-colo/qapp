from task_orchestration.models import Decision, RunState
from task_orchestration.components.processes.task_process import BusinessTask


class NormalExecutionTask(BusinessTask):
    def __init__(self, bridge):
        self.bridge = bridge

    def advance(self, context):
        host = self.bridge.host
        if not host._within_execution_window():
            return Decision(RunState.SUCCEEDED, "normal_window_closed")
        if host._trading_paused:
            return Decision(RunState.WAITING, "user_paused")
        self.bridge.execute()
        if self.bridge.target_satisfied():
            return Decision(RunState.SUCCEEDED, "target_achieved")
        return Decision(RunState.RUNNING, "target_remaining")
