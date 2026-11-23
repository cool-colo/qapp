from task_orchestration.models import Decision, RunState
from task_orchestration.components.processes.task_process import BusinessTask


class BootstrapTask(BusinessTask):
    def __init__(self, bridge):
        self.bridge = bridge
        self.loaded = False

    def advance(self, context):
        try:
            self.bridge.bootstrap_load()
        except Exception as exc:
            return Decision(RunState.WAITING, f"bootstrap_load_failed: {exc}")
        self.loaded = True
        return Decision(RunState.SUCCEEDED, "initialized_from_business_facts")
