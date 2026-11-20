from task_orchestration.models import Proposal


class NormalExecutionTrigger:
    def __init__(self, bridge):
        self.bridge = bridge

    def evaluate(self, now_ns):
        host = self.bridge.host
        if (host._target_version and host._within_execution_window()
                and not host._trading_paused and not self.bridge.target_satisfied()):
            return Proposal(f"{host._clock_date()}:{host._target_version}")
        return None

