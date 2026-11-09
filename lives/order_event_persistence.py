"""Ordered asynchronous persistence for live Nautilus order events."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from typing import Any
from typing import Callable

from backtests.result_writers.live_records import LiveOrderEventRecord
from backtests.result_writers.live_records import LiveOrderRecord
from backtests.result_writers.live_records import LiveTradeRecord


@dataclass(frozen=True)
class OrderEventWrite:
    event_record: LiveOrderEventRecord
    order_record: LiveOrderRecord
    status_time: datetime
    trade_record: LiveTradeRecord | None = None
    enqueued_monotonic: float = 0.0


class AsyncOrderEventWriter:
    """Move order-event MySQL I/O off the Nautilus message-bus callback.

    One worker intentionally serializes bundles: event insertion, current-order
    update, and an optional fill insert commit atomically and in callback order.
    """

    _STOP = object()

    def __init__(
        self,
        writer: Any,
        *,
        logger: Any | None = None,
        now: Callable[[], datetime] = datetime.now,
        retry_initial_secs: float = 0.1,
        retry_max_secs: float = 5.0,
    ) -> None:
        if retry_initial_secs <= 0 or retry_max_secs < retry_initial_secs:
            raise ValueError("invalid order-event persistence retry interval")
        self._writer = writer
        self._logger = logger or logging.getLogger(__name__)
        self._now = now
        self._retry_initial_secs = retry_initial_secs
        self._retry_max_secs = retry_max_secs
        self._queue: queue.Queue[OrderEventWrite | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._queued_at: list[float] = []
        self._accepting = False
        self._pending_count = 0
        self._failed_attempts = 0
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run,
                name="qapp-order-event-writer",
                daemon=True,
            )
            self._thread.start()

    def submit(self, write: OrderEventWrite) -> None:
        enqueued_at = time.monotonic()
        with self._lock:
            if not self._accepting:
                raise RuntimeError("order-event persistence worker is not running")
            self._pending_count += 1
            self._queued_at.append(enqueued_at)
        self._queue.put(replace(write, enqueued_monotonic=enqueued_at))

    def flush(self, timeout_secs: float = 10.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_secs)
        with self._drained:
            while self._pending_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drained.wait(remaining)
            return True

    def stop(self, timeout_secs: float = 10.0) -> bool:
        with self._lock:
            if self._thread is None:
                return True
            self._accepting = False
            thread = self._thread
        self._queue.put(self._STOP)
        thread.join(max(0.0, timeout_secs))
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
        return stopped

    def metrics(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            oldest_age = max(0.0, now - self._queued_at[0]) if self._queued_at else 0.0
            return {
                "pending_count": self._pending_count,
                "oldest_age_secs": oldest_age,
                "failed_attempts": self._failed_attempts,
                "last_error": self._last_error,
                "running": self._thread is not None and self._thread.is_alive(),
            }

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            assert isinstance(item, OrderEventWrite)
            self._persist_with_retry(item)
            with self._drained:
                self._pending_count -= 1
                if self._queued_at:
                    self._queued_at.pop(0)
                if self._pending_count == 0:
                    self._drained.notify_all()

    def _persist_with_retry(self, write: OrderEventWrite) -> None:
        delay = self._retry_initial_secs
        while True:
            event_record = replace(write.event_record, persisted_at=self._now())
            try:
                self._writer.write_realtime_order_event(
                    event_record,
                    write.order_record,
                    write.status_time,
                    write.trade_record,
                )
            except Exception as exc:  # noqa: BLE001 - retried and exposed in health
                with self._lock:
                    self._failed_attempts += 1
                    self._last_error = str(exc)
                self._logger.warning(
                    "live order-event persistence failed; "
                    f"retrying in {delay:.2f}s: {exc}",
                )
                time.sleep(delay)
                delay = min(self._retry_max_secs, delay * 2)
                continue
            with self._lock:
                self._last_error = None
            return
