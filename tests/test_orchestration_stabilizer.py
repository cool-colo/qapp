from concurrent.futures import Future
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, StrategyId, TraderId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs as Events

from task_orchestration.core.orchestrator import TaskOrchestrator
from task_orchestration.models import Decision, Proposal, RunState, TaskContext, TaskSpec
from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer
from trading_orchestration.tasks.bootstrap_task import BootstrapTask
from strategies.async_scheduling import AsyncAwaitableScheduler
from strategies.execution_reconciliation import ExecutionStateReconciler


class CleanupBoundary:
    def __init__(self):
        self.orders = []
        self.futures = []
        self.cancel_window = True
        self.host = SimpleNamespace(
            id=StrategyId('TEST-001'),
            cache=SimpleNamespace(orders_open=lambda: self.orders, orders_inflight=lambda: []),
            replanning=SimpleNamespace(busy=False), replanning_orders=lambda: self.orders,
            _within_cancel_window=lambda: self.cancel_window, _replanning_blocked=False,
            _execution_state_reconciler=SimpleNamespace(is_configured=True, reconcile_once=self.reconcile),
            _async_scheduler=SimpleNamespace(schedule=self.schedule), _execution_activity=0,
            config=SimpleNamespace(cancel_reconverge_delay_secs=0),
            cancel_for_replanning=Mock(side_effect=self.cancel),
        )
        self.bootstrap_load = Mock()
        self.discard_execution_requests = Mock()

    async def reconcile(self):
        pass

    def schedule(self, coroutine):
        coroutine.close()
        future = Future()
        self.futures.append(future)
        return future

    def operations(self):
        return nullcontext()

    def add_order(self, key='order-1'):
        order = LimitOrder(trader_id=TraderId('TEST-001'), strategy_id=self.host.id,
                           instrument_id=InstrumentId.from_str('000001.SZ.QMT'),
                           client_order_id=ClientOrderId(key), order_side=OrderSide.BUY,
                           quantity=Quantity.from_int(100), price=Price.from_str('10.00'),
                           init_id=UUID4(), ts_init=0)
        order.apply(Events.order_submitted(order))
        order.apply(Events.order_accepted(order))
        self.orders.append(order)
        return order

    def cancel(self, order):
        assert self.host._replanning_blocked
        order.apply(Events.order_pending_cancel(order))

    def reject(self, order):
        order.apply(OrderCancelRejected(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=order.instrument_id, client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id, account_id=order.account_id,
            reason='temporarily unavailable', event_id=UUID4(), ts_event=0, ts_init=0))


def context(second):
    return TaskContext(int(second * 1e9), 1, 'run-1', 1, Proposal('test'))


class StabilizerRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.bridge = CleanupBoundary()
        self.stabilizer = NautilusStateStabilizer(self.bridge)

    def test_bootstrap_retries_failed_future_and_admits_next_task(self):
        boot = BootstrapTask(self.bridge)
        engine = TaskOrchestrator(stabilizer_factory=lambda _scope: NautilusStateStabilizer(self.bridge))
        engine.register(TaskSpec('boot', 0), lambda: boot, bootstrap=True)
        process = SimpleNamespace(advance=Mock(return_value=Decision(RunState.RUNNING)))
        engine.register(TaskSpec('next', 1), lambda: process)
        engine.start(0)
        engine.submit('next', Proposal('next'))
        self.bridge.futures[0].set_exception(TimeoutError('transient'))
        engine.tick(1_000_000_000)
        self.assertEqual(engine.lifecycle, 'initializing')
        self.assertIsNone(engine.snapshot()['runs'][0]['final_result'])
        self.assertIsNone(engine._stabilizers["default"].future)
        engine.tick(5_000_000_000)
        self.assertEqual(len(self.bridge.futures), 1)
        engine.tick(6_000_000_000)
        self.bridge.futures[1].set_result(None)
        for second in range(7, 15):
            for future in self.bridge.futures:
                if not future.done():
                    future.set_result(None)
            engine.tick(second * 1_000_000_000)
        self.assertEqual(engine.lifecycle, 'ready')
        self.assertEqual(engine.owner_task(), 'next')
        self.bridge.bootstrap_load.assert_called_once()
        self.assertFalse([r for r in engine.records if r.action == 'COMPONENT_ERROR'])
        failure = next(r for r in engine.records if r.reason == 'reconciliation_failed')
        self.assertEqual(failure.evidence['error'], 'TimeoutError: transient')

    def test_cleanup_failure_keeps_owner_until_successful_retry(self):
        self.bridge.host._execution_state_reconciler.is_configured = False
        engine = TaskOrchestrator(stabilizer_factory=lambda _scope: NautilusStateStabilizer(self.bridge))
        done = SimpleNamespace(advance=lambda _: Decision(RunState.SUCCEEDED),
                               interrupt=lambda _: None, after_stable=lambda _: None)
        engine.register(TaskSpec('boot', 0), lambda: done, bootstrap=True)
        engine.start(0)
        def complete(_):
            self.bridge.host._execution_state_reconciler.is_configured = True
            return Decision(RunState.SUCCEEDED, 'business_complete')
        process = SimpleNamespace(advance=complete, interrupt=lambda _: None, after_stable=lambda _: None)
        engine.register(TaskSpec('work', 1), lambda: process)
        engine.submit('work', Proposal('work'))
        engine.tick(1)
        self.bridge.futures[0].set_exception(RuntimeError('execution-state reconciliation failed'))
        engine.tick(1_000_000_000)
        self.assertEqual(engine.owner_task(), 'work')
        engine.tick(6_000_000_000)
        self.bridge.futures[1].set_result(None)
        engine.tick(7_000_000_000)
        engine.tick(8_000_000_000)
        self.assertIsNone(engine.owner_task())
        self.assertEqual(engine.snapshot()['runs'][-1]['reason'], 'business_complete')

    def test_cancelled_future_retries_but_programming_error_is_not_hidden(self):
        for error in (None, ValueError('invalid contract')):
            with self.subTest(error=error):
                stable = NautilusStateStabilizer(self.bridge)
                stable.stabilize(context(0))
                future = self.bridge.futures[-1]
                if error is None:
                    future.cancel()
                    self.assertEqual(stable.stabilize(context(1)).reason, 'reconciliation_failed')
                    stable.stabilize(context(6))
                    self.assertIsNot(stable.future, future)
                else:
                    future.set_exception(error)
                    with self.assertRaisesRegex(ValueError, 'invalid contract'):
                        stable.stabilize(context(1))
                self.assertIsNot(stable.future, future)

    def test_rejected_cancel_retries_at_deadline_and_then_cleans_up(self):
        order = self.bridge.add_order()
        self.stabilizer.stabilize(context(0))
        self.assertTrue(order.is_pending_cancel)
        self.bridge.reject(order)
        self.assertFalse(order.is_pending_cancel)
        self.stabilizer.stabilize(context(4))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 1)
        self.stabilizer.stabilize(context(5))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 2)
        self.bridge.orders.clear()  # Terminal report updates Nautilus cache.
        self.stabilizer.stabilize(context(6))
        self.bridge.futures[0].set_result(None)
        self.stabilizer.stabilize(context(7))
        self.assertEqual(self.stabilizer.stabilize(context(8)).state, RunState.SUCCEEDED)
        self.assertFalse(self.bridge.host._replanning_blocked)

    def test_exhaustion_and_failed_query_never_rearm_cancellation_budget(self):
        order = self.bridge.add_order()
        for second in (0, 5, 10):
            self.stabilizer.stabilize(context(second))
            self.bridge.reject(order)
        self.stabilizer.stabilize(context(15))
        self.assertEqual(len(self.bridge.futures), 1)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)
        self.stabilizer.stabilize(context(20))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)
        self.bridge.futures[0].set_exception(ConnectionError('unavailable'))
        self.stabilizer.stabilize(context(21))
        self.stabilizer.stabilize(context(26))
        self.bridge.futures[1].set_result(None)
        self.stabilizer.stabilize(context(27))
        self.stabilizer.stabilize(context(31))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)
        self.stabilizer.stabilize(context(32))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)
        waiting = self.stabilizer.stabilize(context(56))
        self.assertEqual(waiting.reason, 'reconciliation_observation_wait')
        self.assertEqual(waiting.evidence['retry_at_ns'], 57_000_000_000)
        self.assertTrue(waiting.evidence['manual_intervention_required'])
        self.assertEqual(waiting.evidence['cancellations']['order-1']['attempts'], 3)
        self.stabilizer.stabilize(context(57))
        self.assertEqual(len(self.bridge.futures), 3)

    def test_pending_request_is_not_duplicated_and_timeout_reconciles(self):
        self.bridge.add_order()
        self.stabilizer.stabilize(context(0))
        self.stabilizer.stabilize(context(5))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 1)
        self.stabilizer.stabilize(context(30))
        self.assertEqual(len(self.bridge.futures), 1)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 1)

    def test_closed_window_blocks_retry_but_not_authoritative_reconciliation(self):
        order = self.bridge.add_order()
        self.stabilizer.stabilize(context(0))
        self.bridge.reject(order)
        self.bridge.cancel_window = False
        self.assertEqual(self.stabilizer.stabilize(context(5)).reason, 'cancellation_window_closed')
        self.stabilizer.stabilize(context(30))
        self.assertEqual(len(self.bridge.futures), 1)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 1)

    def test_no_reconciler_never_treats_exhausted_cancel_as_cleanup_success(self):
        self.bridge.host._execution_state_reconciler.is_configured = False
        order = self.bridge.add_order()
        for second in (0, 5, 10):
            self.stabilizer.stabilize(context(second))
            self.bridge.reject(order)
        result = self.stabilizer.stabilize(context(15))
        self.assertEqual(result.state, RunState.WAITING)
        self.assertEqual(result.reason, 'cancellation_requires_reconciliation')
        self.bridge.orders.clear()
        self.assertEqual(self.stabilizer.stabilize(context(16)).state, RunState.SUCCEEDED)

    def test_order_discovered_after_reconciliation_prevents_success(self):
        self.stabilizer.stabilize(context(0))
        self.bridge.futures[0].set_result(None)
        self.stabilizer.stabilize(context(1))
        self.bridge.add_order()
        self.assertEqual(self.stabilizer.stabilize(context(2)).reason, 'awaiting_terminal_orders')
        self.assertFalse(self.stabilizer.reconciled)

    def test_execution_activity_restarts_quiet_interval(self):
        self.bridge.host._execution_state_reconciler.is_configured = False
        self.bridge.host.config.cancel_reconverge_delay_secs = 2
        self.stabilizer.stabilize(context(0))
        self.bridge.host._execution_activity += 1
        self.stabilizer.stabilize(context(1))
        self.assertEqual(self.stabilizer.stabilize(context(2)).state, RunState.WAITING)
        self.assertEqual(self.stabilizer.stabilize(context(3)).state, RunState.SUCCEEDED)

    def test_real_reconciler_false_result_recovers_without_latching_failure(self):
        scheduler = AsyncAwaitableScheduler()
        reconciler = ExecutionStateReconciler('Asia/Shanghai', scheduler)
        boundary = Mock(side_effect=[False, True])
        reconciler.configure(boundary, '09:15', 30)
        self.bridge.host._async_scheduler = scheduler
        self.bridge.host._execution_state_reconciler = reconciler
        self.stabilizer.stabilize(context(0))
        self.assertEqual(self.stabilizer.stabilize(context(1)).reason, 'reconciliation_failed')
        self.stabilizer.stabilize(context(6))
        self.stabilizer.stabilize(context(7))
        self.assertEqual(self.stabilizer.stabilize(context(8)).state, RunState.SUCCEEDED)
        self.assertEqual(boundary.call_count, 2)

    def exhaust(self, *orders):
        for second in (0, 5, 10):
            self.stabilizer.stabilize(context(second))
            for order in orders:
                self.bridge.reject(order)

    def test_early_rejection_waits_for_entire_batch_before_retry(self):
        first = self.bridge.add_order('first')
        second = self.bridge.add_order('second')
        self.stabilizer.stabilize(context(0))
        self.bridge.reject(first)
        result = self.stabilizer.stabilize(context(5))
        self.assertEqual(result.reason, 'awaiting_cancellation_batch')
        self.assertEqual(result.evidence['pending_orders'], ['second'])
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 2)
        self.assertEqual(self.bridge.futures, [])
        self.bridge.reject(second)
        self.stabilizer.stabilize(context(6))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 4)

    def test_exhausted_member_does_not_query_while_another_is_pending(self):
        first = self.bridge.add_order('first')
        second = self.bridge.add_order('second')
        for instant in (0, 5):
            self.stabilizer.stabilize(context(instant))
            self.bridge.reject(first)
            self.bridge.reject(second)
        self.stabilizer.stabilize(context(10))
        self.bridge.reject(first)
        self.assertEqual(self.stabilizer.stabilize(context(15)).reason, 'awaiting_cancellation_batch')
        self.assertEqual(self.bridge.futures, [])
        self.bridge.reject(second)
        self.assertEqual(self.stabilizer.stabilize(context(16)).reason, 'reconciling')
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 6)

    def test_timeout_of_oldest_request_does_not_skip_younger_pending_request(self):
        self.bridge.add_order('first')
        self.stabilizer.stabilize(context(0))
        second = self.bridge.add_order('second')
        # A pending request inherited from before this order entered the cache.
        second.apply(Events.order_pending_cancel(second))
        self.stabilizer.stabilize(context(10))
        waiting = self.stabilizer.stabilize(context(30))
        self.assertEqual(waiting.reason, 'awaiting_cancellation_batch')
        self.assertEqual(waiting.evidence['timed_out_orders'], ['first'])
        self.assertEqual(self.bridge.futures, [])
        result = self.stabilizer.stabilize(context(40))
        self.assertEqual(result.reason, 'reconciling')
        self.assertEqual(result.evidence['timed_out_orders'], ['first', 'second'])
        self.stabilizer.stabilize(context(41))
        self.assertEqual(len(self.bridge.futures), 1)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 1)

    def test_unchanged_queries_back_off_and_never_rearm_attempts(self):
        order = self.bridge.add_order()
        self.exhaust(order)
        instant = 15
        for interval in (30, 60, 120, 240, 300, 300):
            self.assertEqual(self.stabilizer.stabilize(context(instant)).reason, 'reconciling')
            self.bridge.futures[-1].set_result(None)
            instant += 1
            result = self.stabilizer.stabilize(context(instant))
            deadline = instant + interval
            self.assertEqual(result.evidence['retry_at_ns'], deadline * 1_000_000_000)
            self.bridge.host._execution_activity += 1  # Unrelated event is not progress.
            self.assertEqual(self.stabilizer.stabilize(context(deadline - 1)).reason,
                             'reconciliation_observation_wait')
            self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)
            instant = deadline

    def test_terminal_reports_bypass_observation_delay_but_require_final_query(self):
        order = self.bridge.add_order()
        self.exhaust(order)
        self.stabilizer.stabilize(context(15))
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(16))
        self.bridge.orders.clear()
        self.assertEqual(self.stabilizer.stabilize(context(17)).reason, 'reconciling')
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(18))
        self.assertEqual(self.stabilizer.stabilize(context(19)).state, RunState.SUCCEEDED)

    def test_progress_resets_observation_delay_without_rearming_cancel_budget(self):
        first = self.bridge.add_order('first')
        second = self.bridge.add_order('second')
        self.exhaust(first, second)
        self.stabilizer.stabilize(context(15))
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(16))
        self.bridge.orders.remove(first)
        result = self.stabilizer.stabilize(context(17))
        self.assertEqual(result.reason, 'reconciling')
        self.assertEqual(result.evidence['unchanged_rounds'], 0)
        self.assertEqual(result.evidence['cancellations']['second']['attempts'], 3)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 6)

    def test_new_order_has_own_budget_without_rearming_exhausted_order(self):
        first = self.bridge.add_order('first')
        self.exhaust(first)
        self.stabilizer.stabilize(context(15))
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(16))
        second = self.bridge.add_order('second')
        self.stabilizer.stabilize(context(17))
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 4)
        self.bridge.host.cancel_for_replanning.assert_called_with(second)
        self.assertEqual(self.stabilizer.cancellations['first'].attempts, 3)
        self.assertEqual(self.stabilizer.unchanged_rounds, 1)

    def test_partial_fill_is_progress_without_resetting_cancellation_budget(self):
        order = self.bridge.add_order()
        self.exhaust(order)
        self.stabilizer.stabilize(context(15))
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(16))
        instrument = TestInstrumentProvider.equity('000001.SZ', 'QMT')
        order.apply(Events.order_filled(order, instrument, last_qty=Quantity.from_int(50)))
        result = self.stabilizer.stabilize(context(17))
        self.assertEqual(result.reason, 'reconciling')
        self.assertEqual(result.evidence['unchanged_rounds'], 0)
        self.assertEqual(result.evidence['cancellations']['order-1']['attempts'], 3)
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 3)

    def test_failed_queries_back_off_exponentially_with_a_cap(self):
        instant = 0
        for interval in (5, 10, 20, 40, 80, 160, 300, 300):
            self.stabilizer.stabilize(context(instant))
            self.bridge.futures[-1].set_exception(ConnectionError('unavailable'))
            instant += 1
            result = self.stabilizer.stabilize(context(instant))
            self.assertIsNone(self.stabilizer.future)
            self.assertEqual(result.evidence['retry_at_ns'], (instant + interval) * 1_000_000_000)
            self.assertEqual(self.stabilizer.stabilize(context(instant + interval - 1)).reason,
                             'reconciliation_retry_wait')
            instant += interval
        self.stabilizer.stabilize(context(instant))
        self.bridge.futures[-1].set_result(None)
        self.stabilizer.stabilize(context(instant + 1))
        self.assertEqual(self.stabilizer.stabilize(context(instant + 2)).state, RunState.SUCCEEDED)

    def test_disabled_reconciliation_keeps_cancel_and_quiet_state_requirements(self):
        self.stabilizer = NautilusStateStabilizer(self.bridge, reconciliation_enabled=False)
        self.bridge.host.config.cancel_reconverge_delay_secs = 2
        order = self.bridge.add_order()
        self.exhaust(order)
        result = self.stabilizer.stabilize(context(15))
        self.assertEqual(result.reason, 'cleanup_reconciliation_disabled')
        self.assertFalse(result.evidence['reconciliation_enabled'])
        self.assertTrue(result.evidence['manual_intervention_required'])
        self.assertEqual(self.bridge.futures, [])
        self.bridge.orders.clear()
        self.assertEqual(self.stabilizer.stabilize(context(16)).state, RunState.WAITING)
        self.assertEqual(self.stabilizer.stabilize(context(18)).state, RunState.SUCCEEDED)
        self.assertEqual(self.bridge.futures, [])

    def test_disabled_reconciliation_bootstrap_admits_only_after_terminal_report(self):
        self.bridge.add_order()
        engine = TaskOrchestrator(stabilizer_factory=lambda _scope: NautilusStateStabilizer(
            self.bridge, reconciliation_enabled=False))
        engine.register(TaskSpec('boot', 0), lambda: BootstrapTask(self.bridge), bootstrap=True)
        engine.start(0)
        engine.tick(31_000_000_000)
        self.assertEqual(engine.lifecycle, 'initializing')
        self.bridge.bootstrap_load.assert_not_called()
        self.assertEqual(self.bridge.futures, [])
        self.bridge.orders.clear()
        for instant in (32, 33, 34):
            engine.tick(instant * 1_000_000_000)
        self.assertEqual(engine.lifecycle, 'ready')
        self.bridge.bootstrap_load.assert_called_once()
        self.assertEqual(self.bridge.futures, [])
