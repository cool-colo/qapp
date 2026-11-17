from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math


PRICE_COMPARISON_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReplanTrigger:
    instrument_id: str
    side: str
    reason: str


def price_limit_state(price: float | None, up: float | None, down: float | None) -> str | None:
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    # Float conversion can place the same quoted price just beyond its limit.
    # This absolute CNY tolerance is well below an A-share price tick.
    if up is not None and math.isfinite(up) and up > 0 and price >= up - PRICE_COMPARISON_TOLERANCE:
        return "up_limit"
    if down is not None and math.isfinite(down) and down > 0 and price <= down + PRICE_COMPARISON_TOLERANCE:
        return "down_limit"
    return None


class ReplanTriggerTracker:
    """Latch a blocking direction until it changes or the stock opens again."""

    def __init__(self) -> None:
        self._day = None
        self._seen: dict[str, tuple[str, str]] = {}

    def reset(self) -> None:
        self._day, self._seen = None, {}

    def rearm(self, triggers: list[ReplanTrigger]) -> None:
        """Recheck late triggers against the next target and current prices."""
        for trigger in triggers:
            if self._seen.get(trigger.instrument_id) == (trigger.side, trigger.reason):
                self._seen.pop(trigger.instrument_id)

    def observe(self, day, targets: dict[str, Decimal], quantities: dict[str, Decimal],
                limits: dict[str, tuple[float | None, float | None, float | None]]) -> list[ReplanTrigger]:
        if day != self._day:
            self._day, self._seen = day, {}
        triggers = []
        for instrument_id in set(self._seen) | set(targets):
            price, up, down = limits.get(instrument_id, (None, None, None))
            state = price_limit_state(price, up, down)
            if state is None:
                # Missing data cannot prove that a previous limit has opened.
                if price is not None and math.isfinite(price) and price > 0:
                    self._seen.pop(instrument_id, None)
                continue
            target = targets.get(instrument_id, quantities.get(instrument_id, Decimal(0)))
            current = quantities.get(instrument_id, Decimal(0))
            if target == current:
                continue
            side = "buy" if target > current else "sell"
            key = (side, state)
            if self._seen.get(instrument_id) != key:
                triggers.append(ReplanTrigger(instrument_id, side, state))
                self._seen[instrument_id] = key
        return sorted(triggers, key=lambda trigger: trigger.instrument_id)
