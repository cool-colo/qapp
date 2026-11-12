from __future__ import annotations

from enum import Enum


class ConvergenceTrigger(str, Enum):
    """Typed source for a target-quantity convergence pass."""

    TARGET_UPDATE = "target_update"
    BAR = "bar"
    ORDER_BOOK_DEPTH = "order_book_depth"
    SELL_FILL = "sell_fill"
    CANCEL = "cancel"
    TIMER = "timer"
    MANUAL_SELL = "manual_sell"
    MANUAL_SELL_ALL = "manual_sell_all"
    PREOPEN_LATENCY = "preopen_latency"

    @property
    def is_manual(self) -> bool:
        return self in {self.MANUAL_SELL, self.MANUAL_SELL_ALL}

    @property
    def bypasses_pause(self) -> bool:
        return self.is_manual

    @property
    def waits_for_lock(self) -> bool:
        return self.is_manual
