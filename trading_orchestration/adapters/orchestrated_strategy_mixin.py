from __future__ import annotations

from datetime import date
from strategies.convergence import ConvergenceTrigger


class OrchestratedStrategyMixin:
    """Dispatch existing strategy entrypoints through the composed task runtime."""

    def control_replanning_blocked(self):
        bridge = self.orchestration
        return (super().control_replanning_blocked() or
                (bridge is not None and (bridge.engine.lifecycle != "ready" or
                 bridge.engine.owner_task() not in (None, "normal_execution"))))

    def control_apply_targets(self, targets, target_date, reason):
        if self.orchestration is None:
            return super().control_apply_targets(targets, target_date, reason)
        from strategies.replanning import ReplanningBusyError
        with self._trading_action_lock:
            if self.control_replanning_blocked():
                raise ReplanningBusyError("manual target conflicts with active task")
            suspended = self._convergence_suspended
            self._convergence_suspended = True
            try:
                return super().control_apply_targets(targets, target_date, reason)
            finally:
                self._convergence_suspended = suspended

    def _run_convergence(self, current_date, trigger, ts_event=None, replacement=None):
        bridge = self.orchestration
        if bridge is not None and not bridge.driving:
            return bridge.execution_requested(trigger, ts_event, replacement)
        return super()._run_convergence(current_date, trigger, ts_event, replacement)

    def _within_execution_window(self, ts_event=None):
        bridge = self.orchestration
        if bridge is not None and bridge.profile_windows is not None:
            return bridge.in_windows(bridge.profile_windows, ts_event)
        return super()._within_execution_window(ts_event)

    def execution_book_windows(self):
        if self.orchestration is not None and self.orchestration.profile_windows is not None:
            return self.orchestration.profile_windows
        return super().execution_book_windows()

    def orchestration_reconcile_existing_orders(self, trading_date):
        return super()._reconcile_unfilled_orders(trading_date)

    def _reconcile_unfilled_orders(self, trading_date):
        bridge = self.orchestration
        if bridge is not None and bridge.active_profile is not None:
            return bridge.active_profile.cancellation.reconcile(trading_date)
        return super()._reconcile_unfilled_orders(trading_date)

    def update_target_quantities(self, quantities, target_date, reason, version=None,
                                 bypass_pause=False, signal_date=None, bypass_replanning=False):
        bridge = self.orchestration
        if bridge is not None and not bridge.installing:
            owner = bridge.engine.owner_task()
            if owner not in (None, "bootstrap", "normal_execution"):
                raise RuntimeError("target update conflicts with active task")
        result = super().update_target_quantities(
            quantities, target_date, reason, version, bypass_pause, signal_date, bypass_replanning)
        plan = self.trading_state.planned_models.get(self._target_version)
        if plan is not None:
            self.trading_state.formal_model = plan
        return result

    def submit_order(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().submit_order(*args, **kwargs)

    def _submit_order_quantity(self, trading_date, intent, quantity, reason,
                               slice_index=None, slice_count=None):
        bridge = self.orchestration
        if bridge is not None and bridge.engine.owner_task() == "exposure_correction":
            quantity = bridge.exposure_admission.quantity(
                bridge.snapshot_provider.capture(), str(intent.instrument_id),
                intent.side.name.lower(), quantity)
            if quantity <= 0:
                intent.context.record(skip_reason="exposure_admission_blocked")
                return False
        return super()._submit_order_quantity(trading_date, intent, quantity, reason,
                                             slice_index, slice_count)

    def submit_order_list(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().submit_order_list(*args, **kwargs)

    def modify_order(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().modify_order(*args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().cancel_order(*args, **kwargs)

    def cancel_orders(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().cancel_orders(*args, **kwargs)

    def cancel_all_orders(self, *args, **kwargs):
        if self.orchestration is not None:
            self.orchestration.reject_unowned_operation()
        return super().cancel_all_orders(*args, **kwargs)

