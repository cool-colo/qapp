"""Asynchronous logging of local BigQMT tick-trace message-bus events."""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict
from typing import Callable

from nautilus_trader.adapters.bigqmt.tick_trace import BigQMTTickTrace
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig


class BigQMTTickTraceLogger(Actor):
    """Keep serialization and log submission off the synchronous message bus."""

    _TOPIC = "bigqmt.tick_trace.*"

    def __init__(
        self,
        config: ActorConfig | None = None,
        *,
        queue_capacity: int = 8192,
        write_line: Callable[[str], None] | None = None,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("tick trace queue capacity must be positive")
        super().__init__(config or ActorConfig(component_id="BigQMTTickTraceLogger"))
        self._queue_capacity = queue_capacity
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._write_line = write_line
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._accepting = False
        self._subscribed = False
        self.dropped_events = 0
        self.failed_logs = 0

    def on_start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._stop_event.is_set():
                raise RuntimeError("tick trace logger is still stopping")
            return
        self._queue = queue.Queue(maxsize=self._queue_capacity)
        self._stop_event.clear()
        self.dropped_events = self.failed_logs = 0
        self._thread = threading.Thread(
            target=self._run, name="qapp-tick-trace-logger", daemon=True,
        )
        self._thread.start()
        self._accepting = True
        self.msgbus.subscribe(topic=self._TOPIC, handler=self.on_tick_trace)
        self._subscribed = True

    def on_tick_trace(self, event: BigQMTTickTrace) -> None:
        if not self._accepting:
            return
        received_ns = self.clock.timestamp_ns()
        try:
            self._queue.put_nowait((event, received_ns))
        except queue.Full:
            self.dropped_events += 1

    def on_stop(self) -> None:
        self._accepting = False
        if self._subscribed:
            self.msgbus.unsubscribe(topic=self._TOPIC, handler=self.on_tick_trace)
            self._subscribed = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self.log.warning("Tick trace logger is still draining after the stop timeout")
            else:
                self._thread = None
        if self.dropped_events or self.failed_logs:
            self.log.warning(
                f"Tick trace logger dropped_events={self.dropped_events} "
                f"failed_logs={self.failed_logs}",
            )

    def on_dispose(self) -> None:
        self.on_stop()

    def _run(self) -> None:
        write_line = self.log.info if self._write_line is None else self._write_line
        while True:
            try:
                event, received_ns = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                fields = asdict(event)
                fields["qapp_received_ns"] = received_ns
                fields["dropped_events_total"] = self.dropped_events
                write_line("live_tick_trace " + json.dumps(fields, separators=(",", ":")))
            except Exception:
                # A failed diagnostic log must not abort this worker or trading.
                self.failed_logs += 1
            finally:
                self._queue.task_done()
