from typing import Protocol
from task_orchestration.models import Decision, TaskContext


class StateStabilizer(Protocol):
    def stabilize(self, context: TaskContext) -> Decision: ...

