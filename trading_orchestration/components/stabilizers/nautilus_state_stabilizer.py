from __future__ import annotations

from asyncio import CancelledError as AsyncCancelledError
from concurrent.futures import CancelledError
from dataclasses import dataclass

from task_orchestration.models import Decision, RunState


@dataclass
class _Cancellation:
    attempts: int = 0
    requested_at: int | None = None
    next_retry_at: int = 0


class NautilusStateStabilizer:
    """Advance cleanup on the owner loop, using logical time for all retries."""

    def __init__(self, bridge, *, retry_interval_ns=5_000_000_000,
                 cancel_timeout_ns=30_000_000_000, max_cancel_attempts=3,
                 reconciliation_enabled=True, observation_interval_ns=30_000_000_000,
                 max_backoff_ns=300_000_000_000):
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
               for value in (retry_interval_ns, cancel_timeout_ns, max_cancel_attempts,
                             observation_interval_ns, max_backoff_ns)):
            raise ValueError("cleanup retry limits must be positive integers")
        if not isinstance(reconciliation_enabled, bool):
            raise ValueError("reconciliation_enabled must be boolean")
        if max_backoff_ns < max(retry_interval_ns, observation_interval_ns):
            raise ValueError("maximum backoff must cover retry and observation intervals")
        self.reconciliation_enabled = reconciliation_enabled
        self.observation_interval_ns = observation_interval_ns
        self.max_backoff_ns = max_backoff_ns
        self.observation_retry_at = 0
        self.unchanged_rounds = 0
        self.previous_quantities = {}
        self.bridge = bridge
        self.retry_interval_ns = retry_interval_ns
        self.cancel_timeout_ns = cancel_timeout_ns
        self.max_cancel_attempts = max_cancel_attempts
        self.cancellations: dict[str, _Cancellation] = {}
        self.future = None
        self.activity = None
        self.ready_at = None
        self.reconciled = False
        self.reconciliation_required = False
        self.reconcile_retry_at = 0
        self.reconciliation_failures = 0
        self.reconciliation_error = None

    def _backoff(self, interval, count):
        return min(self.max_backoff_ns, interval * 2 ** min(max(count - 1, 0), 30))

    def _observe(self, orders):
        quantities = {str(order.client_order_id): order.leaves_qty.as_decimal() for order in orders}
        # Cancel rejection/pending transitions and unrelated events are not progress.
        progressed = any(key not in quantities or quantities[key] < quantity
                         for key, quantity in self.previous_quantities.items())
        if progressed:
            self.unchanged_rounds = 0
            self.observation_retry_at = 0
        self.previous_quantities = quantities

    def _evidence(self, orders, now_ns):
        active = {str(order.client_order_id) for order in orders}
        pending = sorted(str(order.client_order_id) for order in orders if order.is_pending_cancel)
        return {
            "reconciliation_enabled": self.reconciliation_enabled,
            "cancellations": {key: {"attempts": value.attempts,
                                    "requested_at_ns": value.requested_at,
                                    "retry_at_ns": value.next_retry_at}
                              for key, value in sorted(self.cancellations.items())},
            "pending_orders": pending,
            "timed_out_orders": [key for key in pending
                                 if self.cancellations[key].requested_at is not None
                                 and now_ns - self.cancellations[key].requested_at >= self.cancel_timeout_ns],
            "manual_intervention_required": any(self.cancellations[key].attempts >= self.max_cancel_attempts
                                                for key in active),
            "unchanged_rounds": self.unchanged_rounds,
        }

    def _request_reconciliation(self, context, orders):
        evidence = self._evidence(orders, context.now_ns)
        if not self.reconciliation_enabled:
            return Decision(RunState.WAITING, "cleanup_reconciliation_disabled", evidence)
        if not self.bridge.host._execution_state_reconciler.is_configured:
            return Decision(RunState.WAITING, "cancellation_requires_reconciliation", evidence)
        if context.now_ns < self.observation_retry_at:
            return Decision(RunState.WAITING, "reconciliation_observation_wait", {
                **evidence, "retry_at_ns": self.observation_retry_at})
        self.reconciliation_required = True
        return self._reconcile(context, orders)

    def _reconcile(self, context, orders):
        host = self.bridge.host
        evidence = {**self._evidence(orders, context.now_ns),
                    "attempt": self.reconciliation_failures + 1}
        if context.now_ns < self.reconcile_retry_at:
            return Decision(RunState.WAITING, "reconciliation_retry_wait", {
                **evidence, "retry_at_ns": self.reconcile_retry_at,
                "error": self.reconciliation_error})
        try:
            if self.future is None:
                work = host._execution_state_reconciler.reconcile_once()
                try:
                    self.future = host._async_scheduler.schedule(work)
                except Exception:
                    work.close()
                    raise
                return Decision(RunState.WAITING, "reconciling", evidence)
            if not self.future.done():
                return Decision(RunState.WAITING, "reconciling", evidence)
            # Consume exactly once, including exceptions and cancelled futures.
            future, self.future = self.future, None
            future.result()
        except (OSError, RuntimeError, CancelledError, AsyncCancelledError) as exc:
            self.reconciled = False
            self.reconciliation_required = True
            self.ready_at = None
            self.reconciliation_failures += 1
            self.reconciliation_error = f"{type(exc).__name__}: {exc}"
            self.reconcile_retry_at = context.now_ns + self._backoff(
                self.retry_interval_ns, self.reconciliation_failures)
            return Decision(RunState.WAITING, "reconciliation_failed", {
                **evidence, "error": self.reconciliation_error,
                "retry_at_ns": self.reconcile_retry_at})
        self.reconciled = not orders
        self.reconciliation_required = False
        self.reconciliation_failures = 0
        self.reconciliation_error = None
        self.ready_at = None
        # A successful query is not evidence of a successful cancellation.
        # Keep the per-order budget for the entire cleanup instance.
        if orders:
            self.unchanged_rounds += 1
            self.observation_retry_at = context.now_ns + self._backoff(
                self.observation_interval_ns, self.unchanged_rounds)
        else:
            self.observation_retry_at = 0
        return Decision(RunState.WAITING, "reconciliation_completed", {
            **self._evidence(orders, context.now_ns),
            "retry_at_ns": self.observation_retry_at})

    def stabilize(self, context):
        host = self.bridge.host
        self.bridge.discard_execution_requests()
        external = [order for order in host.cache.orders_open() + host.cache.orders_inflight()
                    if order.strategy_id != host.id]
        if external:
            return Decision(RunState.WAITING, "external_orders_in_account", {"orders": len(external)})
        orders = host.replanning_orders()
        self._observe(orders)
        for order in orders:
            attempt = self.cancellations.setdefault(str(order.client_order_id), _Cancellation())
            # Also time out pending requests inherited at bootstrap.
            if order.is_pending_cancel and attempt.requested_at is None:
                attempt.requested_at = context.now_ns
        if self.future is not None:
            return self._reconcile(context, orders)
        if orders:
            self.ready_at = None
            self.reconciled = False
            pending = [order for order in orders if order.is_pending_cancel]
            # Drain the whole batch before resending or querying. A timeout is an
            # explicit uncertainty boundary, not confirmation of cancellation.
            if any(context.now_ns - self.cancellations[str(order.client_order_id)].requested_at
                   < self.cancel_timeout_ns for order in pending):
                return Decision(RunState.WAITING, "awaiting_cancellation_batch",
                                self._evidence(orders, context.now_ns))
            if self.reconciliation_required:
                return self._reconcile(context, orders)
            if pending:
                return self._request_reconciliation(context, orders)
            window_open = host._within_cancel_window()
            dispatched = []
            retried = []
            if window_open:
                with self.bridge.operations():
                    old = host._replanning_blocked
                    host._replanning_blocked = True
                    try:
                        for order in orders:
                            key = str(order.client_order_id)
                            attempt = self.cancellations[key]
                            if (attempt.attempts < self.max_cancel_attempts
                                    and context.now_ns >= attempt.next_retry_at):
                                # Reserve before dispatch, including synchronous failures.
                                attempt.attempts += 1
                                attempt.requested_at = context.now_ns
                                attempt.next_retry_at = context.now_ns + self.retry_interval_ns
                                host.cancel_for_replanning(order)
                                dispatched.append(key)
                                if attempt.attempts > 1:
                                    retried.append(key)
                    finally:
                        host._replanning_blocked = old
            if dispatched:
                return Decision(RunState.WAITING,
                                "retrying_cancellations" if retried else "awaiting_terminal_orders", {
                    **self._evidence(orders, context.now_ns), "retried_orders": sorted(retried)})
            attempts = [self.cancellations[str(order.client_order_id)] for order in orders]
            exhausted = any(a.attempts >= self.max_cancel_attempts
                            and context.now_ns >= a.next_retry_at for a in attempts)
            overdue = any(a.requested_at is not None
                          and context.now_ns - a.requested_at >= self.cancel_timeout_ns for a in attempts)
            if exhausted or overdue:
                return self._request_reconciliation(context, orders)
            return Decision(RunState.WAITING,
                            "awaiting_terminal_orders" if window_open else "cancellation_window_closed",
                            self._evidence(orders, context.now_ns))
        # Terminal reports bypass the no-progress observation delay. A final
        # authoritative pass is still required when cleanup reconciliation is on.
        if self.reconciliation_enabled and host._execution_state_reconciler.is_configured:
            if not self.reconciled or self.reconciliation_required:
                self.reconciliation_required = True
                return self._reconcile(context, orders)
        if self.ready_at is None or self.activity != host._execution_activity:
            self.activity = host._execution_activity
            self.ready_at = context.now_ns + int(float(host.config.cancel_reconverge_delay_secs) * 1e9)
        if context.now_ns < self.ready_at:
            return Decision(RunState.WAITING, "settling_execution_events")
        return Decision(RunState.SUCCEEDED, "execution_stable",
                        {"execution_revision": self.activity, "owner_epoch": context.owner_epoch,
                         "reconciliation_enabled": self.reconciliation_enabled})
