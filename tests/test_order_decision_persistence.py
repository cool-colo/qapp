from __future__ import annotations

from datetime import date
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from backtests.result_writers.live_records import LiveOrderDecisionRecord
from backtests.result_writers.live_writer import CREATE_TABLES_SQL
from backtests.result_writers.live_writer import LiveSnapshotWriter
from lives.order_decision_persistence import AsyncOrderDecisionWriter
from lives.snapshot_recorder import SnapshotRecorder
from strategies.target_quantities import OrderDecisionEvent


def _record(decision_id: str = "D-1") -> LiveOrderDecisionRecord:
    timestamp = datetime(2026, 11, 11, 14, 30)
    return LiveOrderDecisionRecord(
        trade_date=date(2026, 11, 11),
        account_id="ACC",
        trader_id="TRADER",
        decision_id=decision_id,
        intent_id="I-1",
        order_chain_id="C-1",
        client_order_id="O-2",
        trigger_client_order_id="O-1",
        decision_type="REPLACEMENT_SUBMIT",
        decision_status="requested",
        reason_code="cancel_reconverge",
        instrument_id="000001.SZ.BIGQMT",
        stock_code="000001.SZ",
        side="buy",
        order_qty=1000,
        order_price=Decimal("10.01"),
        policy_name="SameSideBestBuyPriceStrategy",
        context={"pricing": {"best_bid": Decimal("10.00")}},
        decision_time=timestamp,
        decision_ns=1,
        persisted_at=timestamp,
    )


def test_order_decision_schema_keeps_strategy_details_in_json() -> None:
    ddl = next(sql for sql in CREATE_TABLES_SQL if "`live_order_decision`" in sql)

    assert "`context`" in ddl
    assert "`client_order_id`" in ddl
    assert "`trigger_client_order_id`" in ddl
    assert "`order_chain_id`" in ddl
    assert "`source_same_side_best`" not in ddl
    assert "`mismatch_age_ms`" not in ddl


def test_writer_appends_idempotent_order_decision() -> None:
    connection = MagicMock()
    writer = LiveSnapshotWriter.for_testing(connection, commit=True)

    writer.write_order_decision(_record())

    sql, params = connection.cursor.return_value.execute.call_args.args
    assert "INSERT INTO `live_order_decision`" in sql
    assert "ON DUPLICATE KEY UPDATE `decision_id`" in sql
    assert any('"best_bid": "10.00"' in str(value) for value in params)
    connection.commit.assert_called_once_with()


def test_async_order_decision_writer_retries_and_drains() -> None:
    writer = MagicMock()
    writer.write_order_decision.side_effect = [RuntimeError("db down"), None]
    worker = AsyncOrderDecisionWriter(
        writer,
        retry_initial_secs=0.001,
        retry_max_secs=0.002,
    )
    worker.start()
    worker.submit(_record())

    assert worker.flush(1.0)
    assert worker.stop(1.0)
    assert writer.write_order_decision.call_count == 2
    assert worker.metrics()["failed_attempts"] == 1
    assert worker.metrics()["pending_count"] == 0


def test_snapshot_recorder_maps_strategy_decision_to_live_record() -> None:
    timestamp = datetime(2026, 11, 11, 14, 30)
    decision_writer = MagicMock()
    recorder = SimpleNamespace(
        config=SimpleNamespace(account_id="ACC", trader_id="TRADER"),
        _order_decision_writer=decision_writer,
        _seen_order_decision_ids=set(),
        _ns_to_naive=MagicMock(return_value=timestamp),
        _now_naive=MagicMock(return_value=timestamp),
        _stock_code=MagicMock(return_value="000001.SZ"),
        _int_or_none=SnapshotRecorder._int_or_none,
        _decimal_or_none=SnapshotRecorder._decimal_or_none,
        log=MagicMock(),
    )
    event = OrderDecisionEvent(
        decision_id="D-1",
        trading_date=date(2026, 11, 11),
        decision_ns=1,
        intent_id="I-1",
        order_chain_id="C-1",
        client_order_id="O-2",
        trigger_client_order_id="O-1",
        decision_type="REPLACEMENT_SUBMIT",
        decision_status="requested",
        reason_code="cancel_reconverge",
        instrument_id="000001.SZ.BIGQMT",
        side="buy",
        target_qty=Decimal("3000"),
        order_qty=Decimal("1000"),
        order_price=Decimal("10.01"),
        context={"pricing": {"best_bid": Decimal("10.00")}},
    )

    SnapshotRecorder._on_order_decision(recorder, event)
    SnapshotRecorder._on_order_decision(recorder, event)

    record = decision_writer.submit.call_args.args[0]
    decision_writer.submit.assert_called_once()
    assert record.account_id == "ACC"
    assert record.client_order_id == "O-2"
    assert record.trigger_client_order_id == "O-1"
    assert record.order_chain_id == "C-1"
    assert record.context == event.context
