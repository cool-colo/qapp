from collections.abc import Callable
from datetime import date


class ExistingCancellationPolicy:
    """Preserve existing price deviation, partial-fill grace and retry rules."""

    def __init__(self, reconcile: Callable[[date], None]):
        self._reconcile = reconcile

    def reconcile(self, trading_date: date) -> None:
        self._reconcile(trading_date)
