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

    async def reconcile(self):
        pass

    def schedule(self, coroutine):
        coroutine.close()
        future = Future()
        self.futures.append(future)
        return future

    def operations(self):
        return nullcontext()

    def add_order(self):
        order = LimitOrder(trader_id=TraderId('TEST-001'), strategy_id=self.host.id,
                           instrument_id=InstrumentId.from_str('000001.SZ.QMT'),
                           client_order_id=ClientOrderId('order-1'), order_side=OrderSide.BUY,
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
        engine = TaskOrchestrator()
        engine.register(TaskSpec('boot', 0), lambda: boot, bootstrap=True)
        process = SimpleNamespace(advance=Mock(return_value=Decision(RunState.RUNNING)))
        engine.register(TaskSpec('next', 1), lambda: process)
        engine.start(0)
        engine.submit('next', Proposal('next'))
        self.bridge.futures[0].set_exception(TimeoutError('transient'))
        engine.tick(1_000_000_000)
        self.assertEqual(engine.lifecycle, 'initializing')
        self.assertIsNone(engine.snapshot()['runs'][0]['final_result'])
        self.assertIsNone(boot.stabilizer.future)
        engine.tick(5_000_000_000)
        self.assertEqual(len(self.bridge.futures), 1)
        engine.tick(6_000_000_000)
        self.bridge.futures[1].set_result(None)
        for second in (7, 8, 9):
            engine.tick(second * 1_000_000_000)
        self.assertEqual(engine.lifecycle, 'ready')
        self.assertEqual(engine.owner_task(), 'next')
        self.bridge.bootstrap_load.assert_called_once()
        self.assertFalse([r for r in engine.records if r.action == 'COMPONENT_ERROR'])
        failure = next(r for r in engine.records if r.reason == 'reconciliation_failed')
        self.assertEqual(failure.evidence['error'], 'TimeoutError: transient')

    def test_cleanup_failure_keeps_owner_until_successful_retry(self):
        engine = TaskOrchestrator()
        done = SimpleNamespace(advance=lambda _: Decision(RunState.SUCCEEDED),
                               quiesce=lambda _: Decision(RunState.SUCCEEDED))
        engine.register(TaskSpec('boot', 0), lambda: done, bootstrap=True)
        engine.start(0)
        process = SimpleNamespace(advance=lambda _: Decision(RunState.SUCCEEDED, 'business_complete'),
                                  quiesce=self.stabilizer.stabilize)
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

    def test_exhaustion_reconciles_open_order_and_rearms_bounded_round(self):
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
        self.assertEqual(self.bridge.host.cancel_for_replanning.call_count, 4)

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
