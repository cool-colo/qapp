from typing import Protocol, TypeVar

ResultT = TypeVar("ResultT", contravariant=True)


class OutcomePolicy(Protocol[ResultT]):
    def decide(self, result: ResultT) -> str: ...

