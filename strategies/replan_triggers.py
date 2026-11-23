from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import math
import re
from typing import Protocol
from zoneinfo import ZoneInfo


PRICE_COMPARISON_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReplanTrigger:
    instrument_id: str
    side: str
    reason: str
    kind: str = "price_limit"


def normalize_scheduled_replan_times(values) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("scheduled_replan_times must be a list of clock times")
    normalized = set()
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
            raise ValueError("scheduled_replan_times requires HH:MM or HH:MM:SS strings")
        try:
            clock_time = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid scheduled_replan_times time: {value}") from exc
        if not (time(9, 30) <= clock_time < time(11, 30)
                or time(13) <= clock_time < time(15)):
            raise ValueError("scheduled_replan_times must be inside continuous trading sessions")
        normalized.add(clock_time.isoformat())
    return tuple(sorted(normalized))


def _local_now(host) -> datetime:
    return host.clock.utc_now().astimezone(ZoneInfo(host.config.timezone_name))


class ReplanValidity(Protocol):
    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool: ...


@dataclass(frozen=True)
class ExecutionWindowReplanValidity:
    kind: str = "execution_window"

    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool:
        return (host._clock_date() == trading_date and host._within_execution_window()
                and (not (starting or canceling) or host._within_cancel_window()))


@dataclass(frozen=True)
class SessionReplanValidity:
    scheduled_at: datetime
    expires_at: datetime
    kind: str = "continuous_session"

    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool:
        now = _local_now(host)
        return (now.date() == trading_date == self.scheduled_at.date()
                and self.scheduled_at <= now < self.expires_at)


class PriceLimitReplanTriggerPolicy:
    def __init__(self, tracker: ReplanTriggerTracker) -> None:
        self.tracker = tracker
        self.validity = ExecutionWindowReplanValidity()

    def collect(self, host, trading_date: date, event=None) -> tuple[ReplanTrigger, ...]:
        from nautilus_trader.model.identifiers import InstrumentId

        if (not host.automatic_replanning_allowed()
                or not host._target_version
                or not self.validity.allows(host, trading_date, starting=True)):
            return ()
        desired = host._desired_quantities()
        quantities = {iid: host._current_quantity(InstrumentId.from_str(iid)) for iid in desired}
        limits = {iid: (None if host._market_status.get(iid) is None
                        else host._market_status[iid].last_price, *host._price_limits(iid))
                  for iid in desired.keys() | host._market_status.keys()}
        return tuple(self.tracker.observe(trading_date, desired, quantities, limits))


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
