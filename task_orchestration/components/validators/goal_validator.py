from typing import Protocol, TypeVar

ContextT = TypeVar("ContextT", contravariant=True)
GoalT = TypeVar("GoalT", contravariant=True)


class GoalValidator(Protocol[ContextT, GoalT]):
    def validate(self, context: ContextT, goal: GoalT) -> bool: ...

