from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import math
import re
from typing import ClassVar, Protocol
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
    coalesce_while_busy: bool

    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool: ...


@dataclass(frozen=True)
class ExecutionWindowReplanValidity:
    coalesce_while_busy: ClassVar[bool] = True
    kind: str = "execution_window"

    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool:
        return (host._clock_date() == trading_date and host._within_execution_window()
                and (not (starting or canceling) or host._within_cancel_window()))


@dataclass(frozen=True)
class SessionReplanValidity:
    coalesce_while_busy: ClassVar[bool] = False
    scheduled_at: datetime
    expires_at: datetime
    kind: str = "continuous_session"

    def allows(self, host, trading_date: date, *, starting: bool = False,
               canceling: bool = False) -> bool:
        now = _local_now(host)
        return (now.date() == trading_date == self.scheduled_at.date()
                and self.scheduled_at <= now < self.expires_at)


@dataclass(frozen=True)
class ScheduledReplanTrigger:
    trading_date: date
    scheduled_at: datetime
    expires_at: datetime
    kind: str = "scheduled"

    @property
    def validity(self) -> SessionReplanValidity:
        return SessionReplanValidity(self.scheduled_at, self.expires_at)


class ReplanTriggerPolicy(Protocol):
    def collect(self, host, trading_date: date, event) -> tuple: ...


class PriceLimitReplanTriggerPolicy:
    def __init__(self, tracker: ReplanTriggerTracker) -> None:
        self.tracker = tracker
        self.validity = ExecutionWindowReplanValidity()

    def collect(self, host, trading_date: date, event=None) -> tuple[ReplanTrigger, ...]:
        from nautilus_trader.model.identifiers import InstrumentId

        if (host.replanning.initial_in_progress or not host.automatic_replanning_allowed()
                or not host._target_version
                or not self.validity.allows(host, trading_date, starting=True)):
            return ()
        desired = host._desired_quantities()
        quantities = {iid: host._current_quantity(InstrumentId.from_str(iid)) for iid in desired}
        limits = {iid: (None if host._market_status.get(iid) is None
                        else host._market_status[iid].last_price, *host._price_limits(iid))
                  for iid in desired.keys() | host._market_status.keys()}
        return tuple(self.tracker.observe(trading_date, desired, quantities, limits))


class ScheduledReplanTriggerPolicy:
    """Daily alerts and a separate queue; limit-state latches never see clock events."""

    _ALERT_PREFIX = "MODEL-SCHEDULED-REPLAN-"
    _PENDING_ALERT = "MODEL-SCHEDULED-REPLAN-PENDING"

    def __init__(self, host, times) -> None:
        self.host = host
        self.times = normalize_scheduled_replan_times(times)
        self.next_times: dict[str, datetime] = {}
        self.pending: list[ScheduledReplanTrigger] = []

    def start(self) -> None:
        for clock_time in self.times:
            self._schedule(clock_time)

    def stop(self) -> None:
        for name in self.host.clock.timer_names:
            if name.startswith(self._ALERT_PREFIX):
                self.host.clock.cancel_timer(name)
        self.next_times.clear()
        self.pending.clear()

    def _schedule(self, clock_time: str) -> None:
        host = self.host
        now = _local_now(host)
        next_time = datetime.combine(now.date(), time.fromisoformat(clock_time), now.tzinfo)
        if next_time <= now:
            next_time += timedelta(days=1)
        self.next_times[clock_time] = next_time
        host.clock.set_time_alert(
            name=self._ALERT_PREFIX + clock_time, alert_time=next_time,
            callback=lambda _event: self._on_alert(clock_time), override=True,
        )

    def collect(self, host, trading_date: date,
                event: ScheduledReplanTrigger) -> tuple[ScheduledReplanTrigger, ...]:
        reason = None
        if trading_date not in host._trading_dates:
            reason = "not a trading day"
        elif not host.automatic_replanning_allowed():
            reason = "automatic replanning disabled or paused, or manual targets protected"
        elif host._target_date != trading_date or not host._target_version:
            reason = "no accepted target for this trading day"
        elif not event.validity.allows(host, trading_date):
            reason = "trigger expired"
        if reason is not None:
            host.log.info(f"Scheduled replan skipped: time={event.scheduled_at.isoformat()} reason={reason}")
            return ()
        return (event,)

    def _on_alert(self, clock_time: str) -> None:
        host = self.host
        with host._trading_action_lock:
            if host.replanning.stopped:
                return
            scheduled_at = self.next_times[clock_time]
            self._schedule(clock_time)
            end = time(11, 30) if scheduled_at.time() < time(11, 30) else time(15)
            event = ScheduledReplanTrigger(
                scheduled_at.date(), scheduled_at,
                datetime.combine(scheduled_at.date(), end, scheduled_at.tzinfo),
            )
            triggers = self.collect(host, event.trading_date, event)
            if not triggers:
                return
            if event not in self.pending:
                self.pending.append(event)
            if host.replanning.busy:
                host.log.info(f"Scheduled replan queued: time={scheduled_at.isoformat()}")
            self.drain()

    def wake_pending(self) -> None:
        """Initial planning returns before its caller installs the target."""
        if self.pending and not self.host.replanning.stopped:
            self.host.clock.set_time_alert(
                name=self._PENDING_ALERT,
                alert_time=self.host.clock.utc_now() + timedelta(milliseconds=1),
                callback=lambda _event: self.drain(), override=True,
            )

    def drain(self) -> None:
        host = self.host
        with host._trading_action_lock:
            if host.replanning.stopped or host.replanning.busy or not self.pending:
                return
            events, self.pending = self.pending, []
            valid = [event for event in events if self.collect(host, event.trading_date, event)]
            if valid:
                # Retain every contributing time, but run one fresh planning attempt.
                validity = min(valid, key=lambda event: event.expires_at).validity
                try:
                    request_id = host.submit_automatic_replanning(valid, validity)
                except Exception as exc:
                    host.log.error(f"Scheduled replan request failed: {exc}")
                    return
                if request_id is None:
                    # Initial planning can reserve its lock between collect and
                    # request. Clock events must not join a limit attempt or vanish.
                    self.pending.extend(valid)

    def state(self) -> dict:
        return {"scheduled_replan_next_times": {key: value.isoformat()
                                               for key, value in self.next_times.items()},
                "scheduled_replan_pending": [
                    {"trading_date": str(event.trading_date),
                     "scheduled_at": event.scheduled_at.isoformat(),
                     "expires_at": event.expires_at.isoformat()}
                    for event in self.pending]}


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
