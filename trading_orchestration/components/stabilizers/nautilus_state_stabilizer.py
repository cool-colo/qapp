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
                 cancel_timeout_ns=30_000_000_000, max_cancel_attempts=3):
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
               for value in (retry_interval_ns, cancel_timeout_ns, max_cancel_attempts)):
            raise ValueError("cleanup retry limits must be positive integers")
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

    def _reconcile(self, context):
        host = self.bridge.host
        evidence = {"attempt": self.reconciliation_failures + 1}
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
            self.reconcile_retry_at = context.now_ns + self.retry_interval_ns
            return Decision(RunState.WAITING, "reconciliation_failed", {
                **evidence, "error": self.reconciliation_error,
                "retry_at_ns": self.reconcile_retry_at})
        self.reconciled = True
        self.reconciliation_required = False
        self.reconciliation_failures = 0
        self.reconciliation_error = None
        self.ready_at = None
        # A fresh authoritative pass permits another bounded cancellation round.
        # Do not cancel in the same turn as consuming its result.
        for attempt in self.cancellations.values():
            attempt.attempts = 0
            attempt.requested_at = None
            attempt.next_retry_at = context.now_ns + self.retry_interval_ns
        return Decision(RunState.WAITING, "reconciliation_completed")

    def stabilize(self, context):
        host = self.bridge.host
        external = [order for order in host.cache.orders_open() + host.cache.orders_inflight()
                    if order.strategy_id != host.id]
        if external:
            return Decision(RunState.WAITING, "external_orders_in_account", {"orders": len(external)})
        if host.replanning.busy:
            return Decision(RunState.WAITING, "existing_planning_in_progress")
        if self.future is not None or self.reconciliation_required:
            return self._reconcile(context)
        orders = host.replanning_orders()
        active_ids = {str(order.client_order_id) for order in orders}
        self.cancellations = {key: value for key, value in self.cancellations.items()
                              if key in active_ids}
        if orders:
            self.ready_at = None
            self.reconciled = False
            for order in orders:
                key = str(order.client_order_id)
                attempt = self.cancellations.setdefault(key, _Cancellation())
                # Also time out pending requests inherited at bootstrap.
                if order.is_pending_cancel and attempt.requested_at is None:
                    attempt.requested_at = context.now_ns
            exhausted = any(not order.is_pending_cancel
                            and self.cancellations[str(order.client_order_id)].attempts >= self.max_cancel_attempts
                            and context.now_ns >= self.cancellations[str(order.client_order_id)].next_retry_at
                            for order in orders)
            overdue = any(a.requested_at is not None and
                          context.now_ns - a.requested_at >= self.cancel_timeout_ns
                          for a in self.cancellations.values())
            if exhausted or overdue:
                if host._execution_state_reconciler.is_configured:
                    self.reconciliation_required = True
                    return self._reconcile(context)
                return Decision(RunState.WAITING, "cancellation_requires_reconciliation", {
                    "orders": sorted(active_ids), "retry_exhausted": exhausted,
                    "confirmation_overdue": overdue})
            if not host._within_cancel_window():
                return Decision(RunState.WAITING, "cancellation_window_closed",
                                {"orders": len(orders)})
            with self.bridge.operations():
                retried = []
                old = host._replanning_blocked
                host._replanning_blocked = True
                try:
                    for order in orders:
                        key = str(order.client_order_id)
                        attempt = self.cancellations[key]
                        if not order.is_pending_cancel and context.now_ns >= attempt.next_retry_at:
                            # Rearm only when Nautilus no longer reports pending cancel.
                            # Reserve the attempt before dispatch, including synchronous failures.
                            attempt.attempts += 1
                            attempt.requested_at = context.now_ns
                            attempt.next_retry_at = context.now_ns + self.retry_interval_ns
                            host.cancel_for_replanning(order)
                            if attempt.attempts > 1:
                                retried.append(key)
                finally:
                    host._replanning_blocked = old
            return Decision(RunState.WAITING,
                            "retrying_cancellations" if retried else "awaiting_terminal_orders", {
                "retried_orders": sorted(retried),
                "cancellations": {key: {"attempts": value.attempts,
                                         "retry_at_ns": value.next_retry_at}
                                  for key, value in sorted(self.cancellations.items())}})
        reconciler = host._execution_state_reconciler
        if not self.reconciled and reconciler.is_configured:
            self.reconciliation_required = True
            return self._reconcile(context)
        if self.ready_at is None or self.activity != host._execution_activity:
            self.activity = host._execution_activity
            self.ready_at = context.now_ns + int(float(host.config.cancel_reconverge_delay_secs) * 1e9)
        if context.now_ns < self.ready_at:
            return Decision(RunState.WAITING, "settling_execution_events")
        return Decision(RunState.SUCCEEDED, "execution_stable",
                        {"execution_revision": self.activity, "owner_epoch": context.owner_epoch})
