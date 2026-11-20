from task_orchestration.models import Decision, RunState
from task_orchestration.components.validators.goal_validator import GoalValidator
from task_orchestration.components.stabilizers.state_stabilizer import StateStabilizer


class ExposureExecutor:
    def __init__(self, bridge, *, validator: GoalValidator, stabilizer: StateStabilizer):
        self.bridge = bridge
        self.validator = validator
        self.stabilizer = stabilizer
        self.plan = None

    def apply(self, context, goal):
        snapshot = self.bridge.snapshot_provider.capture()
        if not self.bridge.engine.owns(context) or not self.validator.validate(snapshot, goal):
            return Decision(RunState.FAILED, "temporary_target_stale")
        self.bridge.install_quantities(dict(goal.quantities),
                                       f"exposure:{context.run_id}:{context.event_seq}",
                                       "exposure_correction", temporary=True)
        self.plan = goal
        return Decision(RunState.RUNNING, "temporary_target_applied")

    def start(self, context):
        if not self.bridge.engine.owns(context) or not self.bridge.exposure_allowed():
            return Decision(RunState.WAITING, "execution_not_permitted")
        self.bridge.execute(exposure=True)
        return Decision(RunState.RUNNING, "single_side_execution")

    def hold(self, context):
        result = self.stabilizer.stabilize(context)
        if result.state == RunState.SUCCEEDED:
            self.bridge.restore_formal()
        return result

    def snapshot(self):
        return self.bridge.execution_state()

