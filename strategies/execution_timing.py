"""In-process execution diagnostics; no scheduling or persistence side effects."""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any
from typing import Callable
from typing import Iterator


@dataclass(frozen=True)
class TimingPoint:
    timestamp_ns: int
    monotonic_ns: int


@dataclass
class ConvergenceTiming:
    convergence_id: str
    trading_date: date
    trigger: str
    scheduled: bool
    configured_delay_ns: int
    pending_at_request: int
    points: dict[str, TimingPoint]
    pending_at_start: int = 0
    status: str = "completed"
    reason: str = "converged"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConvergenceEvent:
    convergence_id: str
    trading_date: date
    trigger: str
    status: str
    reason: str
    requested_ns: int
    started_ns: int | None
    execution_started_ns: int | None
    finished_ns: int
    queue_duration_ns: int | None
    execution_duration_ns: int | None
    total_duration_ns: int
    configured_delay_ns: int
    delay_elapsed_ns: int | None
    pending_at_request: int
    pending_at_start: int
    context: dict[str, Any]

    def log_payload(self, strategy_id: str) -> dict[str, Any]:
        """One JSON-safe diagnostic line, without market/order payloads."""
        return {
            "strategy_id": strategy_id,
            "trading_date": self.trading_date.isoformat(),
            "convergence_id": self.convergence_id,
            "trigger": self.trigger,
            "status": self.status,
            "reason": self.reason,
            "requested_ns": self.requested_ns,
            "started_ns": self.started_ns,
            "execution_started_ns": self.execution_started_ns,
            "finished_ns": self.finished_ns,
            "queue_duration_ns": self.queue_duration_ns,
            "execution_duration_ns": self.execution_duration_ns,
            "total_duration_ns": self.total_duration_ns,
            "configured_delay_ns": self.configured_delay_ns,
            "delay_elapsed_ns": self.delay_elapsed_ns,
            "pending_at_request": self.pending_at_request,
            "pending_at_start": self.pending_at_start,
            "context": self.context,
        }


class ExecutionTiming:
    """Track queued tasks across threads and the active synchronous cycle per thread."""

    def __init__(
        self,
        clock_ns: Callable[[], int],
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._clock_ns = clock_ns
        self._monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._local = threading.local()
        self._local.active = None

    def point(self) -> TimingPoint:
        return TimingPoint(self._clock_ns(), self._monotonic_ns())

    @property
    def active(self) -> ConvergenceTiming | None:
        # Thread-local state is explicitly initialized for every callback thread.
        if "active" not in self._local.__dict__:
            self._local.active = None
        return self._local.active

    def request(
        self, trading_date: date, trigger: str, *, scheduled: bool = False,
        delay_secs: float = 0.0,
    ) -> ConvergenceTiming:
        point = self.point()
        convergence_id = uuid.uuid4().hex
        with self._lock:
            if scheduled:
                self._pending.add(convergence_id)
            pending = len(self._pending)
        return ConvergenceTiming(
            convergence_id, trading_date, trigger, scheduled,
            int(delay_secs * 1_000_000_000), pending, {"requested": point},
        )

    def start(self, timing: ConvergenceTiming) -> bool:
        with self._lock:
            if "finished" in timing.points:
                return False
            timing.points["started"] = self.point()
            self._pending.discard(timing.convergence_id)
            timing.pending_at_start = len(self._pending)
        return True

    @contextmanager
    def activate(self, timing: ConvergenceTiming) -> Iterator[None]:
        previous = self.active
        self._local.active = timing
        try:
            yield
        finally:
            self._local.active = previous

    def mark(self, name: str) -> None:
        timing = self.active
        if timing is not None:
            timing.points[name] = self.point()

    def skip(self, reason: str) -> None:
        timing = self.active
        if timing is not None:
            timing.status, timing.reason = "skipped", reason

    def decision_context(self) -> dict[str, Any]:
        timing = self.active
        if timing is None:
            return {}
        return {
            "convergence_id": timing.convergence_id,
            "convergence_trigger": timing.trigger,
            "requested_ns": timing.points["requested"].timestamp_ns,
            "started_ns": timing.points["started"].timestamp_ns,
            "execution_started_ns": timing.points["execution_started"].timestamp_ns,
        }

    def finish(
        self, timing: ConvergenceTiming, *, cancel_unstarted: bool = False,
    ) -> ConvergenceEvent | None:
        with self._lock:
            if "finished" in timing.points:
                return None
            if cancel_unstarted:
                if "started" in timing.points:
                    return None
                timing.status, timing.reason = "cancelled", "task_cancelled_before_start"
            timing.points["finished"] = self.point()
            self._pending.discard(timing.convergence_id)
        points = timing.points

        def timestamp(name: str) -> int | None:
            return points[name].timestamp_ns if name in points else None

        def duration(start: str, end: str) -> int | None:
            if start not in points or end not in points:
                return None
            return points[end].monotonic_ns - points[start].monotonic_ns

        context = deepcopy(timing.context)
        context["scheduled"] = timing.scheduled
        context["stages"] = {
            name: {
                "timestamp_ns": point.timestamp_ns,
                "elapsed_ns": point.monotonic_ns - points["requested"].monotonic_ns,
            }
            for name, point in points.items()
        }
        return ConvergenceEvent(
            convergence_id=timing.convergence_id,
            trading_date=timing.trading_date,
            trigger=timing.trigger,
            status=timing.status,
            reason=timing.reason,
            requested_ns=points["requested"].timestamp_ns,
            started_ns=timestamp("started"),
            execution_started_ns=timestamp("execution_started"),
            finished_ns=points["finished"].timestamp_ns,
            queue_duration_ns=duration("requested", "started"),
            execution_duration_ns=duration("execution_started", "finished"),
            total_duration_ns=points["finished"].monotonic_ns - points["requested"].monotonic_ns,
            configured_delay_ns=timing.configured_delay_ns,
            delay_elapsed_ns=duration("started", "execution_started"),
            pending_at_request=timing.pending_at_request,
            pending_at_start=timing.pending_at_start,
            context=context,
        )
