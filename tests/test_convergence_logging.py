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


def test_convergence_is_one_json_log_line_with_id_and_all_timing_fields():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    event = _event()
    TargetQuantityStrategy._emit_convergence(strategy, event)
    messages = [args[0] for args, kwargs in strategy.log.infos if args[0].startswith("live_convergence ")]
    assert len(messages) == 1
    assert "\n" not in messages[0]
    payload = json.loads(messages[0].removeprefix("live_convergence "))
    assert payload == event.log_payload(str(strategy.id))
    assert payload["trading_date"] == "2026-11-13"
    assert payload["convergence_id"] == event.convergence_id
    assert payload["status"] == "skipped"
    assert payload["context"]["stages"]["execution_started"]["timestamp_ns"] == event.execution_started_ns
    assert all("CREATE TABLE IF NOT EXISTS `live_convergence`" not in sql for sql in CREATE_TABLES_SQL)


def test_duplicate_completion_does_not_emit_another_log_line():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    tracker = ExecutionTiming(lambda: 1_000_000_000)
    timing = tracker.request(date(2026, 11, 13), "timer")
    TargetQuantityStrategy._emit_convergence(strategy, tracker.finish(timing))
    TargetQuantityStrategy._emit_convergence(strategy, tracker.finish(timing))
    assert len(strategy.log.infos) == 1


def test_log_id_matches_persisted_decision_and_cycle_logging_does_not_enqueue_db_record():
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
    recorder._before_time = recorder._after_time = None
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
        message = next(args[0] for args, kwargs in strategy.log.infos if args[0].startswith("live_convergence "))
        payload = json.loads(message.removeprefix("live_convergence "))
        assert record.context["execution_timing"]["convergence_id"] == payload["convergence_id"]
        submission = payload["context"]["submissions"][0]
        assert submission["decision_id"] == record.decision_id
        assert submission["client_order_id"] == record.client_order_id
        assert "submit_duration_ns" in submission
    finally:
        recorder.on_stop()
