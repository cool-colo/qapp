from typing import Protocol, TypeVar

ContextT = TypeVar("ContextT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


class GoalPlanner(Protocol[ContextT, ResultT]):
    def plan(self, context: ContextT) -> ResultT: ...

