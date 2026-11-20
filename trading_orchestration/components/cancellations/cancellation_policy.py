from datetime import date
from typing import Protocol


class CancellationPolicy(Protocol):
    def reconcile(self, trading_date: date) -> None: ...
