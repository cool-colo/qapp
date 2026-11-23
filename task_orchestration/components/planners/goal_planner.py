from typing import Protocol, TypeVar

ContextT = TypeVar("ContextT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


class GoalPlanner(Protocol[ContextT, ResultT]):
    def plan(self, context: ContextT) -> ResultT: ...



class InterruptibleGoalPlanner(GoalPlanner[ContextT, ResultT], Protocol[ContextT, ResultT]):
    def abort(self, reason: str = 'planning_interrupted') -> None: ...
