from __future__ import annotations

import queue
import threading
from datetime import date
from datetime import datetime
from unittest.mock import MagicMock

from backtests.result_writers.live_records import LiveOrderEventRecord
from backtests.result_writers.live_records import LiveOrderRecord
from backtests.result_writers.live_writer import LiveSnapshotWriter
from backtests.result_writers.live_writer import CREATE_TABLES_SQL
from lives.order_event_persistence import AsyncOrderEventWriter
from lives.order_event_persistence import OrderEventWrite


def _bundle(event_id: str, second: int = 0) -> OrderEventWrite:
    timestamp = datetime(2026, 7, 9, 9, 30, second)
    order = LiveOrderRecord(
        trade_date=date(2026, 7, 9),
        write_time=timestamp,
        account_id="ACC",
        trader_id="TRADER",
        client_order_id="O-1",
        instrument_id="000001.SZ.BIGQMT",
        stock_code="000001.SZ",
        status="accepted",
    )
    event = LiveOrderEventRecord(
        trade_date=order.trade_date,
        account_id=order.account_id,
        trader_id=order.trader_id,
        event_id=event_id,
        event_type="OrderAccepted",
        client_order_id=order.client_order_id,
        instrument_id=order.instrument_id,
        stock_code=order.stock_code,
        status=order.status,
        broker_event_time=timestamp,
        broker_event_ns=1,
        broker_time_kind="order_time",
        local_event_time=timestamp,
        local_event_ns=2,
        local_received_time=timestamp,
        persisted_at=timestamp,
    )
    return OrderEventWrite(event, order, timestamp)


def test_async_order_event_writer_preserves_fifo_order_and_drains() -> None:
    writer = MagicMock()
    worker = AsyncOrderEventWriter(
        writer,
        retry_initial_secs=0.001,
        retry_max_secs=0.002,
    )
    worker.start()
    worker.submit(_bundle("E-1", 1))
    worker.submit(_bundle("E-2", 2))

    assert worker.flush(1.0)
    assert worker.stop(1.0)
    assert [call.args[0].event_id for call in writer.write_realtime_order_event.call_args_list] == [
        "E-1",
        "E-2",
    ]
    assert worker.metrics()["pending_count"] == 0


def test_async_order_event_writer_retries_idempotent_bundle() -> None:
    writer = MagicMock()
    writer.write_realtime_order_event.side_effect = [RuntimeError("db down"), None]
    worker = AsyncOrderEventWriter(
        writer,
        retry_initial_secs=0.001,
        retry_max_secs=0.002,
    )
    worker.start()
    worker.submit(_bundle("E-1"))

    assert worker.flush(1.0)
    assert worker.stop(1.0)
    assert writer.write_realtime_order_event.call_count == 2
    assert worker.metrics()["failed_attempts"] == 1
    assert worker.metrics()["last_error"] is None


def test_stop_cannot_overtake_an_accepted_submission() -> None:
    class BlockingPutQueue(queue.Queue):
        def __init__(self) -> None:
            super().__init__()
            self.write_put_started = threading.Event()
            self.allow_write_put = threading.Event()
            self.stop_put_started = threading.Event()

        def put(self, item, block=True, timeout=None) -> None:
            if isinstance(item, OrderEventWrite):
                self.write_put_started.set()
                assert self.allow_write_put.wait(1.0)
            else:
                self.stop_put_started.set()
            super().put(item, block, timeout)

    writer = MagicMock()
    worker = AsyncOrderEventWriter(writer)
    controlled_queue = BlockingPutQueue()
    worker._queue = controlled_queue
    worker.start()

    submit_thread = threading.Thread(target=worker.submit, args=(_bundle("E-1"),))
    submit_thread.start()
    assert controlled_queue.write_put_started.wait(1.0)

    stop_results: list[bool] = []
    stop_call_started = threading.Event()

    def stop_worker() -> None:
        stop_call_started.set()
        stop_results.append(worker.stop(1.0))

    stop_thread = threading.Thread(target=stop_worker)
    stop_thread.start()
    assert stop_call_started.wait(1.0)

    # stop() must remain behind submit()'s acceptance/enqueue critical section.
    assert not controlled_queue.stop_put_started.wait(0.05)
    controlled_queue.allow_write_put.set()
    submit_thread.join(1.0)
    stop_thread.join(1.0)

    assert not submit_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_results == [True]
    writer.write_realtime_order_event.assert_called_once()
    assert worker.metrics()["pending_count"] == 0


def test_writer_commits_event_and_realtime_summary_in_one_transaction() -> None:
    cursor = MagicMock()
    cursor.rowcount = 1
    connection = MagicMock()
    connection.cursor.return_value = cursor
    writer = LiveSnapshotWriter.for_testing(connection, commit=True)
    bundle = _bundle("E-1")

    writer.write_realtime_order_event(
        bundle.event_record,
        bundle.order_record,
        bundle.status_time,
    )

    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert "INSERT INTO `live_order_event`" in sql[0]
    assert "INSERT INTO `live_order_realtime`" in sql[1]
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()


def test_writer_duplicate_event_does_not_regress_realtime_summary() -> None:
    cursor = MagicMock()
    cursor.rowcount = 0
    connection = MagicMock()
    connection.cursor.return_value = cursor
    writer = LiveSnapshotWriter.for_testing(connection, commit=True)
    bundle = _bundle("E-1")

    writer.write_realtime_order_event(
        bundle.event_record,
        bundle.order_record,
        bundle.status_time,
    )

    assert cursor.execute.call_count == 1
    connection.commit.assert_called_once_with()


def test_order_event_schema_keeps_broker_and_local_clocks() -> None:
    ddl = next(sql for sql in CREATE_TABLES_SQL if "`live_order_event`" in sql)
    assert "`broker_event_time`" in ddl
    assert "`broker_event_ns`" in ddl
    assert "`local_event_time`" in ddl
    assert "`local_received_time`" in ddl
    assert "DATETIME(6)" in ddl
