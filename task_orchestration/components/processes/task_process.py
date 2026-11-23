from typing import Protocol
from task_orchestration.models import Decision, TaskContext


class TaskProcess(Protocol):
    """Business work starts only after the framework's stable-state boundary."""

    def advance(self, context: TaskContext) -> Decision: ...
    def interrupt(self, context: TaskContext) -> None:
        """Invalidate asynchronous business work; never submit or cancel orders."""
        ...
    def after_stable(self, context: TaskContext) -> None:
        """Release temporary business state after framework cleanup; no orders."""
        ...


class BusinessTask:
    """Optional no-op business lifecycle hooks, with no state cleanup dependency."""

    def interrupt(self, context: TaskContext) -> None:
        pass

    def after_stable(self, context: TaskContext) -> None:
        pass
