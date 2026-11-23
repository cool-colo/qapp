from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Protocol, Callable
from zoneinfo import ZoneInfo


class TimerClock(Protocol):
    def now_ns(self) -> int: ...
    def arm(self, name: str, at_ns: int, callback: Callable[[], None]) -> None: ...
    def cancel(self, name: str) -> None: ...


@dataclass(frozen=True)
class ScheduledOccurrence:
    trigger_id: str
    scheduled_at_ns: int
    fired_at_ns: int
    expires_at_ns: int | None

    @property
    def key(self):
        return f'{self.trigger_id}:{self.scheduled_at_ns}'


class ScheduledTrigger:
    """Push-only daily calendar trigger. No domain state or evaluate method."""

    def __init__(self, trigger_id, clock: TimerClock, timezone, schedule, emit):
        self.trigger_id, self.clock = trigger_id, clock
        self.zone, self.emit = ZoneInfo(timezone), emit
        self.schedule = tuple(sorted({(time.fromisoformat(at).isoformat(),
                                       None if until is None else time.fromisoformat(until).isoformat())
                                      for at, until in schedule}))
        self.next_times = {}
        self.last_occurrence = None
        self._generation = 0
        self._active = False
        self._last_fired = {}

    def start(self):
        self.stop()
        self._active = True
        now = datetime.fromtimestamp(self.clock.now_ns() / 1e9, self.zone)
        for at, until in self.schedule:
            due = datetime.combine(now.date(), time.fromisoformat(at), self.zone)
            if due < now:
                due += timedelta(days=1)
            self._arm(at, until, due)

    def stop(self):
        self._active = False
        self._generation += 1
        for at in self.next_times:
            self.clock.cancel(f'scheduled:{self.trigger_id}:{at}')
        self.next_times.clear()

    def _arm(self, at, until, due):
        generation = self._generation
        stamp = int(due.timestamp() * 1e9)
        self.next_times[at] = due
        def fired():
            if not self._active or self._generation != generation:
                return
            now_ns = self.clock.now_ns()
            following = due + timedelta(days=1)
            while int(following.timestamp() * 1e9) <= now_ns:
                following += timedelta(days=1)
            self._arm(at, until, following)
            if self._last_fired.get(at) == stamp:
                return
            self._last_fired[at] = stamp
            expiry = None if until is None else int(datetime.combine(
                due.date(), time.fromisoformat(until), self.zone).timestamp() * 1e9)
            occurrence = ScheduledOccurrence(self.trigger_id, stamp, now_ns, expiry)
            self.last_occurrence = occurrence
            self.emit(occurrence)
        self.clock.arm(f'scheduled:{self.trigger_id}:{at}', stamp, fired)

    def state(self):
        last = self.last_occurrence
        return {'type': 'scheduled', 'active': self._active,
                'next_times': {key: value.isoformat() for key, value in self.next_times.items()},
                'last_scheduled_at_ns': None if last is None else last.scheduled_at_ns,
                'last_fired_at_ns': None if last is None else last.fired_at_ns}
