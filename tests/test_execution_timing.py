from __future__ import annotations

import asyncio
from concurrent.futures import Future
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nautilus_trader.model.enums import OrderSide

from strategies.convergence import ConvergenceTrigger
from strategies.execution_timing import ExecutionTiming
from strategies.target_quantities import TargetQuantityStrategy
from strategies.target_quantities import TargetQuantityStrategyConfig
from tests import test_target_quantities as fixtures


DAY = date(2026, 7, 2)


class Timeline:
    def __init__(self) -> None:
        self.epoch = fixtures.china_ts_ns("2026-07-02 14:30:01")
        self.monotonic = 0

    def advance(self, ns: int) -> None:
        self.epoch += ns
        self.monotonic += ns

    def tracker(self) -> ExecutionTiming:
        return ExecutionTiming(lambda: self.epoch, lambda: self.monotonic)


class HoldingScheduler:
    def __init__(self) -> None:
        self.coroutines = []

    def schedule(self, coroutine):
        self.coroutines.append(coroutine)
        return Future()


class TimedStrategy(TargetQuantityStrategy):
    """Real Nautilus strategy construction with a controlled execution boundary."""

    def __init__(self, timeline: Timeline) -> None:
        super().__init__(TargetQuantityStrategyConfig(
            auction_enabled=False, instrument_ids=[], bar_types={},
        ))
        self.timeline = timeline
        self._execution_timing = timeline.tracker()
        self.work = lambda: timeline.advance(2_000_000)
        self.convergence_events = []

    def _emit_convergence(self, event):
        # This controlled runtime has no registered node logger; logging is
        # exercised separately with the strategy's real emission method.
        if event is not None:
            self.convergence_events.append(event)

    def _run_convergence(self, current_date, trigger, ts_event=None, replacement=None):
        self.work()
        return True


def test_task_queueing_delay_and_execution_are_separate_even_after_clock_adjustment():
    timeline = Timeline()
    strategy = TimedStrategy(timeline)
    scheduler = HoldingScheduler()
    strategy._async_scheduler = scheduler
    strategy._schedule_convergence(DAY, ConvergenceTrigger.CANCEL, delay_secs=0.1)
    requested_ns = timeline.epoch
    timeline.advance(500_000_000)

    async def sleep(seconds):
        assert seconds == 0.1
        timeline.advance(130_000_000)  # Includes a 30ms event-loop resume delay.
        timeline.epoch -= 2_000_000_000  # Wall clock adjustment must not affect durations.

    with patch("strategies.target_quantities.asyncio.sleep", sleep):
        asyncio.run(scheduler.coroutines.pop())

    event = strategy.convergence_events[-1]
    assert event.requested_ns == requested_ns
    assert event.queue_duration_ns == 500_000_000
    assert event.configured_delay_ns == 100_000_000
    assert event.delay_elapsed_ns == 130_000_000
    assert event.execution_duration_ns == 2_000_000
    assert event.total_duration_ns == 632_000_000
    assert event.finished_ns < event.requested_ns
    assert event.pending_at_request == 1
    assert event.pending_at_start == 0


def test_pending_count_reports_other_queued_tasks():
    timeline = Timeline()
    strategy = TimedStrategy(timeline)
    scheduler = HoldingScheduler()
    strategy._async_scheduler = scheduler
    strategy._schedule_convergence(DAY, ConvergenceTrigger.TIMER)
    strategy._schedule_convergence(DAY, ConvergenceTrigger.SELL_FILL)
    timeline.advance(1_000_000_000)
    for coroutine in scheduler.coroutines:
        asyncio.run(coroutine)
    first, second = strategy.convergence_events
    assert (first.pending_at_request, first.pending_at_start) == (1, 1)
    assert (second.pending_at_request, second.pending_at_start) == (2, 0)
    assert second.queue_duration_ns == 1_002_000_000


@pytest.mark.parametrize("reason", ["trading_paused", "outside_execution_window", "lock_busy", "no_target_version"])
def test_skipped_cycles_are_recorded_without_orders(reason):
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    if reason == "trading_paused":
        strategy._trading_paused = True
    elif reason == "outside_execution_window":
        strategy._within_execution_window = lambda ts_event=None: False
        strategy._within_cancel_window = lambda ts_event=None: False
    elif reason == "lock_busy":
        strategy._converge_lock.acquire()
    try:
        strategy._converge_to_target(DAY, ConvergenceTrigger.TIMER)
    finally:
        if reason == "lock_busy":
            strategy._converge_lock.release()
    assert len(strategy.convergence_events) == 1
    event = strategy.convergence_events[0]
    assert (event.status, event.reason) == ("skipped", reason)
    assert event.execution_duration_ns is not None
    assert not strategy.submitted_orders
    assert strategy._execution_timing.active is None


def test_exception_and_task_cancellation_record_completion_and_restore_context():
    timeline = Timeline()
    strategy = TimedStrategy(timeline)

    def fail():
        timeline.advance(8_000_000)
        raise ValueError("execution failed")

    strategy.work = fail
    with pytest.raises(ValueError):
        strategy._converge_to_target(DAY, ConvergenceTrigger.TIMER)
    failed = strategy.convergence_events[-1]
    assert (failed.status, failed.reason) == ("failed", "execution_exception")
    assert failed.execution_duration_ns == 8_000_000
    assert strategy._execution_timing.active is None

    async def cancel(seconds):
        raise asyncio.CancelledError

    with patch("strategies.target_quantities.asyncio.sleep", cancel):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(strategy._converge_async(DAY, ConvergenceTrigger.CANCEL, 0.1))
    cancelled = strategy.convergence_events[-1]
    assert cancelled.status == "cancelled"
    assert cancelled.execution_started_ns is None
    assert cancelled.execution_duration_ns is None


def test_scheduler_failure_records_request_without_inventing_start_time():
    timeline = Timeline()
    strategy = TimedStrategy(timeline)

    class FailingScheduler:
        def schedule(self, coroutine):
            raise RuntimeError("loop closed")

    strategy._async_scheduler = FailingScheduler()
    with pytest.raises(RuntimeError):
        strategy._schedule_convergence(DAY, ConvergenceTrigger.TIMER)
    event = strategy.convergence_events[-1]
    assert event.reason == "schedule_exception"
    assert event.started_ns is None
    assert event.queue_duration_ns is None
    assert strategy._execution_timing.request(DAY, "probe").pending_at_request == 0


def test_cancellation_before_coroutine_entry_records_and_removes_pending_request():
    timeline = Timeline()
    strategy = TimedStrategy(timeline)
    strategy._async_scheduler = HoldingScheduler()
    future = strategy._schedule_convergence(DAY, ConvergenceTrigger.TIMER)
    timeline.advance(25_000_000)
    assert future.cancel()
    event = strategy.convergence_events[-1]
    assert event.reason == "task_cancelled_before_start"
    assert event.started_ns is None
    assert event.total_duration_ns == 25_000_000
    assert strategy._execution_timing.request(DAY, "probe").pending_at_request == 0


def test_depth_observation_distinguishes_delivery_delay_from_cached_book_age():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    timeline = Timeline()
    strategy._execution_timing = timeline.tracker()
    strategy._trading_day = DAY
    strategy._depth_converged_today = True
    depth = SimpleNamespace(
        instrument_id=fixtures.INST_A,
        ts_event=timeline.epoch - 1_000_000_000,
        ts_init=timeline.epoch - 500_000_000,
        bids=[SimpleNamespace(price=10, size=100)],
        asks=[SimpleNamespace(price=10.01, size=100)],
    )
    strategy.on_order_book_depth(depth)
    observed_ns = timeline.epoch
    timeline.advance(2_000_000_000)
    ctx = strategy._build_price_context(
        strategy.cache.instrument(fixtures.INST_A), fixtures.INST_A, OrderSide.BUY, Decimal(100),
    )
    assert ctx.ts_event == depth.ts_event
    assert ctx.ts_init == depth.ts_init
    assert ctx.observed_ns == observed_ns
    assert ctx.observed_ns - ctx.ts_init == 500_000_000
    assert ctx.captured_ns - ctx.observed_ns == 2_000_000_000
    assert ctx.observed_to_capture_ns == 2_000_000_000
    assert ctx.book_source == "depth"


def test_quote_cache_without_matching_callback_has_no_fabricated_observation_time():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    timeline = Timeline()
    strategy._execution_timing = timeline.tracker()
    strategy._depth_books.clear()
    quote = SimpleNamespace(
        instrument_id=fixtures.INST_A, ts_event=timeline.epoch, ts_init=timeline.epoch,
        bid_price=10, ask_price=10.01, bid_size=100, ask_size=100,
    )
    strategy.cache.quote_tick = lambda instrument_id: quote
    assert strategy._quote_snapshot(fixtures.INST_A).observed_ns is None
    strategy.on_quote_tick(quote)
    assert strategy._quote_snapshot(fixtures.INST_A).observed_ns == timeline.epoch
    quote.ts_init += 1
    assert strategy._quote_snapshot(fixtures.INST_A).observed_ns is None


def test_timer_retains_source_event_time_and_captures_request_before_queueing():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    timeline = Timeline()
    strategy._execution_timing = timeline.tracker()
    scheduler = HoldingScheduler()
    strategy._async_scheduler = scheduler
    timer_event_ns = timeline.epoch - 500_000_000
    strategy._on_converge_timer(SimpleNamespace(ts_event=timer_event_ns))
    timeline.advance(250_000_000)
    asyncio.run(scheduler.coroutines.pop())
    event = strategy.convergence_events[-1]
    assert event.context["timer_event_ns"] == timer_event_ns
    assert event.requested_ns - timer_event_ns == 500_000_000
    assert event.queue_duration_ns == 250_000_000


def test_real_convergence_links_each_slice_to_cycle_and_keeps_decisions_immutable():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy(order_slice_notional="1200")
    strategy._target_quantities = {str(fixtures.INST_A): Decimal(300)}
    strategy._target_version = "V-1"
    timeline = Timeline()
    strategy._execution_timing = timeline.tracker()
    original_submit = strategy.submit_order

    def submit(order):
        timeline.advance(7_000_000)
        original_submit(order)

    strategy.submit_order = submit
    strategy._converge_to_target(DAY, ConvergenceTrigger.TIMER)
    event = strategy.convergence_events[-1]
    decisions = [e for e in strategy.order_decision_events if e.decision_type == "SUBMIT_REQUESTED"]
    assert len(decisions) == len(strategy.submitted_orders) == 3
    submissions = event.context["submissions"]
    for index, (decision, submission) in enumerate(zip(decisions, submissions)):
        timing = decision.context["execution_timing"]
        assert timing["convergence_id"] == event.convergence_id
        assert "validation_ns" in timing
        assert "pricing_finished_ns" in timing
        assert timing["order_created_ns"] <= decision.decision_ns
        assert submission["decision_id"] == decision.decision_id
        assert submission["client_order_id"] == decision.client_order_id
        assert submission["slice_index"] == decision.context["slice_index"] == index
        assert submission["submit_duration_ns"] == 7_000_000
        assert submission["status"] == "returned"
    assert decisions[0].context["execution_timing"]["slice_started_ns"] < decisions[1].context["execution_timing"]["slice_started_ns"]
    stages = event.context["stages"]
    assert list(stages) == [
        "requested", "started", "execution_started", "window_checked", "lock_acquired",
        "cancel_check_finished", "account_scan_started", "account_scan_finished", "intents_finished",
        "summary_finished", "sells_finished", "buys_finished", "achievement_check_finished", "finished",
    ]


def test_submit_exception_duration_excludes_failure_reporting_and_releases_lock():
    strategy = fixtures.TargetQuantityStrategyTest().make_strategy()
    strategy._target_quantities = {str(fixtures.INST_A): Decimal(100)}
    strategy._target_version = "V-1"
    timeline = Timeline()
    strategy._execution_timing = timeline.tracker()

    def submit(order):
        timeline.advance(7_000_000)
        raise RuntimeError("submission failed")

    strategy.submit_order = submit
    strategy.add_order_decision_listener(lambda event: timeline.advance(20_000_000))
    with pytest.raises(RuntimeError):
        strategy._converge_to_target(DAY, ConvergenceTrigger.TIMER)
    event = strategy.convergence_events[-1]
    assert event.status == "failed"
    assert event.context["submissions"][0]["submit_duration_ns"] == 7_000_000
    assert event.context["submissions"][0]["status"] == "failed"
    assert strategy._converge_lock.acquire(blocking=False)
    strategy._converge_lock.release()
