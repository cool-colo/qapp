from task_orchestration.models import Decision, RunState
from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer


class ManualExecutionTask:
    """Keep the existing manual trigger's pause bypass and execution window."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.stabilizer = NautilusStateStabilizer(bridge)

    def advance(self, context):
        if not self.bridge.host._within_execution_window():
            return Decision(RunState.WAITING, "manual_execution_window_closed")
        self.bridge.execute(trigger=context.proposal.payload)
        return Decision(RunState.SUCCEEDED if self.bridge.target_satisfied() else RunState.RUNNING,
                        "manual_target_evaluated")

    def quiesce(self, context):
        return self.stabilizer.stabilize(context)
