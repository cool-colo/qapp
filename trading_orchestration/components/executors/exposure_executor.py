from task_orchestration.models import Decision, RunState
from task_orchestration.components.validators.goal_validator import GoalValidator


class ExposureExecutor:
    def __init__(self, bridge, *, validator: GoalValidator):
        self.bridge = bridge
        self.validator = validator
        self.plan = None

    def apply(self, context, goal):
        snapshot = self.bridge.snapshot_provider.capture_planning()
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

    def release(self, context):
        self.bridge.restore_formal()
        self.plan = None

    def snapshot(self):
        return self.bridge.execution_state()

