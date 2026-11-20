from typing import Protocol
from task_orchestration.models import Decision, TaskContext


class TaskProcess(Protocol):
    """One nonblocking task instance; cleanup retains ownership until confirmed."""

    def advance(self, context: TaskContext) -> Decision: ...
    def quiesce(self, context: TaskContext) -> Decision: ...

