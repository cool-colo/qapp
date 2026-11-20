from task_orchestration.models import Decision, RunState
from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer


class BootstrapTask:
    def __init__(self, bridge):
        self.bridge = bridge
        self.stabilizer = NautilusStateStabilizer(bridge)
        self.loaded = False

    def advance(self, context):
        stable = self.stabilizer.stabilize(context)
        if stable.state != RunState.SUCCEEDED:
            return stable
        try:
            self.bridge.bootstrap_load()
        except Exception as exc:
            return Decision(RunState.WAITING, f"bootstrap_load_failed: {exc}")
        self.loaded = True
        return Decision(RunState.SUCCEEDED, "initialized_from_business_facts")

    def quiesce(self, context):
        return self.stabilizer.stabilize(context)

