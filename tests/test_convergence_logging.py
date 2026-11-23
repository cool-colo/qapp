from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

from backtests.result_writers.live_records import LiveOrderDecisionRecord
from backtests.result_writers.live_writer import CREATE_TABLES_SQL
from lives.snapshot_recorder import SnapshotRecorder
from lives.snapshot_recorder import SnapshotRecorderConfig
from strategies.execution_timing import ExecutionTiming
from strategies.target_quantities import TargetQuantityStrategy
from strategies.convergence import ConvergenceTrigger
from tests import test_target_quantities as fixtures


def _event():
    tracker = ExecutionTiming(lambda: 1_000_000_000, lambda: 10)
    timing = tracker.request(date(2026, 11, 13), "timer", scheduled=True)
    tracker.start(timing)
    with tracker.activate(timing):
        tracker.mark("execution_started")
        tracker.skip("target_already_achieved")
    return tracker.finish(timing)


def test_convergence_payload_serializes_id_and_all_timing_fields():
    event = _event()
    payload = json.loads(json.dumps(event.log_payload("TEST-STRATEGY")))
    assert payload == event.log_payload("TEST-STRATEGY")
    assert payload["strategy_id"] == "TEST-STRATEGY"
    assert payload["trading_date"] == "2026-11-13"
    assert payload["convergence_id"] == event.convergence_id
    assert payload["status"] == "skipped"
    assert payload["context"]["stages"]["execution_started"]["timestamp_ns"] == event.execution_started_ns
    assert all("CREATE TABLE IF NOT EXISTS `live_convergence`" not in sql for sql in CREATE_TABLES_SQL)


def test_duplicate_completion_does_not_create_another_event():
    tracker = ExecutionTiming(lambda: 1_000_000_000)
    timing = tracker.request(date(2026, 11, 13), "timer")
    event = tracker.finish(timing)
    assert event is not None
    assert tracker.finish(timing) is None


def test_convergence_id_matches_persisted_decision_and_emission_does_not_enqueue_db_record():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    strategy._target_quantities = {str(fixtures.INST_A): Decimal(100)}
    strategy._target_version = "V-1"
    strategy._converge_to_target(date(2026, 7, 2), ConvergenceTrigger.TIMER)
    event = strategy.convergence_events[-1]
    decisions = MagicMock()
    recorder = SnapshotRecorder(
        SnapshotRecorderConfig(account_id="ACC", trader_id="TRADER"),
        writer=MagicMock(), strategy_ref=strategy,
        order_event_writer=MagicMock(), order_decision_writer=decisions,
    )
    recorder._before_auction_time = recorder._before_time = recorder._after_time = None
    recorder._catch_up_on_start = lambda: None
    recorder._subscribe_order_events = lambda: None
    recorder._now_naive = lambda: datetime(2026, 7, 2)
    try:
        recorder.on_start()
        decisions.submit.assert_called_once()
        record = decisions.submit.call_args.args[0]
        assert isinstance(record, LiveOrderDecisionRecord)
        TargetQuantityStrategy._emit_convergence(strategy, event)
        decisions.submit.assert_called_once()  # Logging never enters the MySQL queue.
        payload = event.log_payload(str(strategy.id))
        assert record.context["execution_timing"]["convergence_id"] == payload["convergence_id"]
        submission = payload["context"]["submissions"][0]
        assert submission["decision_id"] == record.decision_id
        assert submission["client_order_id"] == record.client_order_id
        assert "submit_duration_ns" in submission
    finally:
        recorder.on_stop()
