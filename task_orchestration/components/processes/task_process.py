from typing import Protocol
from task_orchestration.models import Decision, TaskContext


class TaskProcess(Protocol):
    """Business work starts after framework admission under the task's cleanup policy."""

    def advance(self, context: TaskContext) -> Decision: ...
    def interrupt(self, context: TaskContext) -> None:
        """Invalidate asynchronous business work; never submit or cancel orders."""
        ...
    def after_stable(self, context: TaskContext) -> None:
        """Release temporary business state after framework cleanup; no orders."""
        ...
    def after_cleanup_skipped(self, context: TaskContext) -> None:
        """Release temporary state on skipped normal exit; does not prove stability."""
        ...


class BusinessTask:
    """Optional no-op business lifecycle hooks, with no state cleanup dependency."""

    def interrupt(self, context: TaskContext) -> None:
        pass

    def after_stable(self, context: TaskContext) -> None:
        pass

    def after_cleanup_skipped(self, context: TaskContext) -> None:
        pass
