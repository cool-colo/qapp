from typing import Protocol
from task_orchestration.models import Proposal


class TriggerPolicy(Protocol):
    def evaluate(self, now_ns: int) -> Proposal | None: ...

