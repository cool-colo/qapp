from typing import Protocol, TypeVar

ContextT = TypeVar("ContextT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


class ProgressAssessor(Protocol[ContextT, ResultT]):
    def assess(self, context: ContextT) -> ResultT: ...

