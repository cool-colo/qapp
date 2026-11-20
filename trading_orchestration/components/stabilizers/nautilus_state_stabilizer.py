from __future__ import annotations

from task_orchestration.models import Decision, RunState


class NautilusStateStabilizer:
    """Nonblocking cancellation, reconciliation, then a logical quiet interval."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.cancelled = set()
        self.future = None
        self.activity = None
        self.ready_at = None
        self.reconciled = False

    def stabilize(self, context):
        host = self.bridge.host
        external = [order for order in host.cache.orders_open() + host.cache.orders_inflight()
                    if order.strategy_id != host.id]
        if external:
            return Decision(RunState.WAITING, "external_orders_in_account", {"orders": len(external)})
        if host.replanning.busy:
            return Decision(RunState.WAITING, "existing_planning_in_progress")
        orders = host.replanning_orders()
        if orders:
            self.ready_at = None
            self.reconciled = False
            if not host._within_cancel_window():
                return Decision(RunState.WAITING, "cancellation_window_closed",
                                {"orders": len(orders)})
            with self.bridge.operations():
                old = host._replanning_blocked
                host._replanning_blocked = True
                try:
                    for order in orders:
                        key = str(order.client_order_id)
                        if key not in self.cancelled and not order.is_pending_cancel:
                            host.cancel_for_replanning(order)
                            self.cancelled.add(key)
                finally:
                    host._replanning_blocked = old
            return Decision(RunState.WAITING, "awaiting_terminal_orders")
        reconciler = host._execution_state_reconciler
        if not self.reconciled and reconciler.is_configured:
            if self.future is None:
                self.future = host._async_scheduler.schedule(reconciler.reconcile_once())
                self.activity = host._execution_activity
                return Decision(RunState.WAITING, "reconciling")
            if not self.future.done():
                return Decision(RunState.WAITING, "reconciling")
            self.future.result()
            self.future = None
            self.reconciled = True
            self.ready_at = None
        if self.ready_at is None or self.activity != host._execution_activity:
            self.activity = host._execution_activity
            self.ready_at = context.now_ns + int(float(host.config.cancel_reconverge_delay_secs) * 1e9)
        if context.now_ns < self.ready_at:
            return Decision(RunState.WAITING, "settling_execution_events")
        return Decision(RunState.SUCCEEDED, "execution_stable",
                        {"execution_revision": self.activity, "owner_epoch": context.owner_epoch})

