from __future__ import annotations

import json
import threading
from dataclasses import asdict
from dataclasses import replace

import pytest

from lives.tick_trace_logging import BigQMTTickTraceLogger
from nautilus_trader.adapters.bigqmt.tick_trace import BigQMTTickTrace
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock as NautilusTestClock
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio


def event():
    return BigQMTTickTrace(
        instrument_id="000001.SZ.BIGQMT", data_kind="depth10",
        ts_event=1700000000000000000, ts_init=1700000000100000000,
        qmt_callback_ns=1700000000010000000,
        pipe_publish_ns=1700000000020000000,
        proxy_pipe_received_ns=1700000000030000000,
        redis_publish_ns=1700000000040000000,
        adapter_redis_received_ns=1700000000050000000,
        adapter_callback_ns=1700000000060000000,
    )


def make_logger(write_line, queue_capacity=8192):
    clock = NautilusTestClock()
    clock.set_time(1700000000110000000)
    bus = MessageBus(trader_id=TraderId("TRACE-TEST"), clock=clock)
    cache = Cache()
    portfolio = Portfolio(msgbus=bus, cache=cache, clock=clock)
    actor = BigQMTTickTraceLogger(write_line=write_line, queue_capacity=queue_capacity)
    actor.register_base(portfolio=portfolio, msgbus=bus, cache=cache, clock=clock)
    return actor, bus, clock


def publish(bus, data):
    bus.publish(topic=f"bigqmt.tick_trace.{data.instrument_id}", msg=data, external_pub=False)


def payload(line):
    assert line.startswith("live_tick_trace ")
    assert "\n" not in line
    return json.loads(line.removeprefix("live_tick_trace "))


def test_callback_logs_all_original_timestamps_on_background_thread_and_unsubscribes():
    lines = []
    writer_threads = []

    def write_line(line):
        lines.append(line)
        writer_threads.append(threading.get_ident())

    actor, bus, clock = make_logger(write_line)
    source = event()
    actor.start()
    actor.on_start()  # No second subscription or worker.
    publish(bus, source)
    actor.stop()
    assert actor._thread is None
    assert len(lines) == 1
    assert writer_threads[0] != threading.get_ident()
    fields = payload(lines[0])
    assert fields["ts_event"] == source.ts_event
    assert fields["ts_init"] == source.ts_init
    assert fields["instrument_id"] == source.instrument_id
    assert fields["data_kind"] == "depth10"
    original_fields = asdict(source)
    for name in (
        "qmt_callback_ns", "pipe_publish_ns", "proxy_pipe_received_ns",
        "redis_publish_ns", "adapter_redis_received_ns", "adapter_callback_ns",
    ):
        assert fields[name] == original_fields[name]
    assert fields["qapp_received_ns"] == clock.timestamp_ns()
    assert fields["dropped_events_total"] == 0
    publish(bus, source)
    assert len(lines) == 1
    actor.dispose()


def test_slow_logger_does_not_block_callback_and_queue_overload_is_counted():
    entered = threading.Event()
    release = threading.Event()
    lines = []

    def write_line(line):
        entered.set()
        assert release.wait(2.0)
        lines.append(line)

    actor, bus, clock = make_logger(write_line, queue_capacity=1)
    actor.on_start()
    try:
        publish(bus, event())
        assert entered.wait(2.0)
        clock.set_time(1700000000120000000)
        publish(bus, replace(event(), data_kind="quote"))
        clock.set_time(1700000000990000000)
        publish(bus, event())
        assert actor.dropped_events == 1
        assert actor._queue.qsize() == 1
    finally:
        release.set()
        actor.on_stop()
    assert len(lines) == 2
    fields = payload(lines[1])
    assert fields["qapp_received_ns"] == 1700000000120000000
    assert fields["data_kind"] == "quote"
    assert fields["dropped_events_total"] == 1


def test_failed_log_does_not_stop_worker_or_later_events():
    lines = []
    attempts = []

    def write_line(line):
        attempts.append(line)
        if len(attempts) == 1:
            raise OSError("test logger failure")
        lines.append(line)

    actor, bus, _clock = make_logger(write_line)
    actor.on_start()
    publish(bus, event())
    publish(bus, replace(event(), ts_init=1700000000200000000))
    actor.on_stop()
    assert actor.failed_logs == 1
    assert len(lines) == 1
    assert payload(lines[0])["ts_init"] == 1700000000200000000


def test_partial_timestamps_remain_null_and_dispose_stops_worker():
    lines = []
    actor, bus, _clock = make_logger(lines.append)
    actor.on_start()
    publish(bus, replace(event(), pipe_publish_ns=None, redis_publish_ns=None))
    actor.on_dispose()
    assert actor._thread is None
    assert payload(lines[0])["pipe_publish_ns"] is None
    assert payload(lines[0])["redis_publish_ns"] is None


def test_logger_restarts_without_old_queue_or_duplicate_subscription():
    lines = []
    actor, bus, _clock = make_logger(lines.append)
    for _ in range(2):
        actor.on_start()
        publish(bus, event())
        actor.on_stop()
    assert len(lines) == 2


def test_invalid_queue_capacity_fails_fast():
    with pytest.raises(ValueError, match="must be positive"):
        BigQMTTickTraceLogger(queue_capacity=0)
