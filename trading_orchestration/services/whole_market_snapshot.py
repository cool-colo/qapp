from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, asdict
from datetime import timedelta
from types import MappingProxyType
from typing import Protocol, Mapping
import pandas as pd

from strategies.tick_snapshot import TickSnapshot


@dataclass(frozen=True)
class SnapshotBatch:
    request_id: str
    requested_at_ns: int
    completed_at_ns: int
    ticks: Mapping[str, TickSnapshot]

    def fields(self):
        return {key: asdict(tick) for key, tick in self.ticks.items()}


class SnapshotProvider(Protocol):
    def request_fresh(self, reason: str) -> Future: ...
    def latest_snapshot(self) -> Mapping[str, TickSnapshot]: ...


class WholeMarketSnapshotService:
    """Serialized data acquisition; each consumer owns an immutable batch result.

    Runtime marshals completions onto the owner loop. No trading decisions or
    execution-state cleanup belong to this service.
    """

    def __init__(self, config, runtime):
        self.config, self.runtime = config, runtime
        self.source = None
        self._latest = MappingProxyType({})
        self.updated_at_ns = None
        self.final_success_dates = set()
        self._queue = deque()
        self._active = None
        self._sequence = 0
        self._generation = 0
        self._stopped = False
        self._timers = set()

    def configure_source(self, source):
        self.source = source

    def latest_snapshot(self):
        return self._latest

    def final_refresh_succeeded(self, day):
        return day in self.final_success_dates

    def clear(self):
        self._latest = MappingProxyType({})
        self.updated_at_ns = None

    def request_fresh(self, reason):
        result = Future()
        generation = self._generation
        def enqueue():
            if generation != self._generation:
                result.cancel()
                return
            if result.cancelled():
                return
            if self._stopped or self.source is None:
                result.set_exception(RuntimeError('whole-market snapshot source is unavailable'))
                return
            self._sequence += 1
            self._queue.append((f'market-{self._sequence}', reason, self.runtime.now_ns(), result))
            self._drain()
        self.runtime.dispatch(enqueue)
        return result

    def _drain(self):
        if self._active is not None or self._stopped:
            return
        while self._queue:
            request = self._queue.popleft()
            if request[3].cancelled():
                continue
            self._active = request
            generation = self._generation
            try:
                future = self.runtime.fetch(self.source)
            except Exception as exc:
                self._active = None
                request[3].set_exception(exc)
                continue
            future.add_done_callback(lambda done: self.runtime.dispatch(
                lambda: self._complete(done, request, generation)))
            return

    def _complete(self, work, request, generation):
        if generation != self._generation:
            return
        self._active = None
        key, reason, requested_at, result = request
        try:
            fields = work.result()
            # Cancelled consumers cannot publish late data into a subsequent task.
            if not result.cancelled():
                batch = self.publish(fields, reason, request_id=key, requested_at_ns=requested_at)
                result.set_result(batch)
        except Exception as exc:
            if not result.cancelled():
                result.set_exception(exc)
        self._drain()

    def publish(self, fields, reason, *, request_id='replay', requested_at_ns=None):
        if not isinstance(fields, dict) or not fields:
            raise ValueError('whole-market snapshot is empty or invalid')
        now = self.runtime.now_ns()
        ticks = MappingProxyType({str(key): TickSnapshot.from_fields(value) for key, value in fields.items()})
        batch = SnapshotBatch(request_id, now if requested_at_ns is None else requested_at_ns, now, ticks)
        self._latest, self.updated_at_ns = ticks, now
        final = self.config.whole_market_final_refresh_time
        local = self.runtime.local_now()
        if final and local.time() >= pd.Timestamp(final).time():
            self.final_success_dates.add(local.date())
        self.runtime.info(f'whole-market full-tick fetch ({reason}): {len(ticks)} instruments request={request_id}')
        return batch

    def refresh(self, reason, on_complete=None, *, require_fresh=False):
        # Periodic refreshes do not pile up; explicit consumers get their own batch.
        if self._active is not None and not require_fresh:
            result = self._active[3]
        else:
            result = self.request_fresh(reason)
        if on_complete is not None:
            def completed(done):
                try:
                    batch = done.result()
                except Exception as exc:
                    on_complete('failed', {'error': str(exc)})
                else:
                    on_complete('success', {'instruments': len(batch.ticks), 'error': None})
            result.add_done_callback(completed)
        return result

    def start(self):
        self._stopped = False
        if not self.config.whole_market_full_tick_enabled or self.source is None:
            return
        self.refresh('start')
        interval = float(self.config.whole_market_full_tick_interval_secs)
        if interval > 0:
            name = 'WHOLE-MARKET-REFRESH'
            self._timers.add(name)
            generation = self._generation
            self.runtime.clock.set_timer(name, timedelta(seconds=interval),
                                         callback=lambda _: self._periodic(generation), fire_immediately=False)
        for name, value, reason in (
            ('WHOLE-MARKET-PREFETCH', self.config.whole_market_full_tick_time, 'prefetch'),
            ('WHOLE-MARKET-FINAL', self.config.whole_market_final_refresh_time, 'final'),
        ):
            if value:
                self._daily(name, value, reason)

    def _periodic(self, generation):
        from strategies.cancellation import parse_cancel_windows
        def refresh():
            if self._stopped or generation != self._generation:
                return
            now = self.runtime.local_now().time()
            if any(start <= now < end for start, end in parse_cancel_windows(self.config.whole_market_refresh_windows)):
                self.refresh('refresh')
        self.runtime.dispatch(refresh)

    def _daily(self, name, value, reason):
        now = pd.Timestamp(self.runtime.local_now())
        h, m = map(int, value.split(':'))
        due = now.normalize() + pd.Timedelta(hours=h, minutes=m)
        if due <= now:
            due += pd.Timedelta(days=1)
        self._timers.add(name)
        generation = self._generation
        def fire(_):
            def advance():
                if not self._stopped and generation == self._generation:
                    self._daily(name, value, reason)
                    self.refresh(reason)
            self.runtime.dispatch(advance)
        self.runtime.clock.set_time_alert(name=name, alert_time=due, callback=fire, override=True)

    def stop(self):
        self._stopped = True
        self._generation += 1
        for name in self._timers:
            if name in self.runtime.clock.timer_names:
                self.runtime.clock.cancel_timer(name)
        self._timers.clear()
        if self._active is not None:
            self._active[3].cancel()
        for request in self._queue:
            request[3].cancel()
        self._queue.clear()
        self._active = None
