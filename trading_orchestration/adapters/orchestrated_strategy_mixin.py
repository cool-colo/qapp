from __future__ import annotations

import hashlib
import json

class OrchestratedStrategyMixin:
    """Dispatch existing strategy entrypoints through the composed task runtime."""

    def control_replanning_blocked(self):
        bridge = self.orchestration
        return (super().control_replanning_blocked() or
                (bridge.engine.lifecycle != "ready" or
                 bridge.engine.owner_task() not in (None, "normal_execution")))

    def control_apply_targets(self, targets, target_date, reason):
        from strategies.replanning import ReplanningBusyError
        from strategies.convergence import ConvergenceTrigger
        from strategies.target_quantities import target_version
        from strategies.trading_control import SELL_ALL_REASON
        from trading_orchestration.contexts.manual_target import ManualTarget
        with self._trading_action_lock:
            if self.control_replanning_blocked():
                raise ReplanningBusyError("manual target conflicts with active task")
            quantities = tuple(sorted(targets.items()))
            digest = hashlib.sha256(json.dumps(
                [(iid, str(qty)) for iid, qty in quantities], separators=(",", ":")
            ).encode()).hexdigest()[:16]
            target = ManualTarget(quantities, target_date, reason,
                                  f"{target_version(target_date, targets, reason)}-{digest}",
                                  ConvergenceTrigger.MANUAL_SELL_ALL if reason == SELL_ALL_REASON
                                  else ConvergenceTrigger.MANUAL_SELL)
            self.orchestration.request("manual_execution", target, key=target.version,
                                       expires_ns=self.orchestration.deadline())

    def control_converge(self, current_date, trigger):
        # control_apply_targets already queued the immutable manual request.
        self.orchestration.wake()

    def _run_convergence(self, current_date, trigger, ts_event=None, replacement=None):
        bridge = self.orchestration
        if (not bridge.driving or bridge._operation_state.mode != "execute"
                or bridge._operation_state.execution_depth):
            return bridge.execution_requested(trigger, ts_event, replacement)
        bridge._operation_state.execution_depth += 1
        try:
            return super()._run_convergence(current_date, trigger, ts_event, replacement)
        finally:
            bridge._operation_state.execution_depth -= 1

    def _schedule_convergence(self, current_date, trigger, delay_secs=0.0,
                              replacement=None, *, timer_event_ns=None):
        return self.orchestration.execution_requested(
            trigger, replacement=replacement, delay_secs=delay_secs)

    def _within_execution_window(self, ts_event=None):
        bridge = self.orchestration
        if bridge.profile_windows is not None:
            return bridge.in_windows(bridge.profile_windows, ts_event)
        return super()._within_execution_window(ts_event)

    def execution_book_windows(self):
        if self.orchestration.profile_windows is not None:
            return self.orchestration.profile_windows
        return super().execution_book_windows()

    def orchestration_reconcile_existing_orders(self, trading_date):
        return super()._reconcile_unfilled_orders(trading_date)

    def _reconcile_unfilled_orders(self, trading_date):
        bridge = self.orchestration
        if bridge.active_profile is not None:
            return bridge.active_profile.cancellation.reconcile(trading_date)
        return super()._reconcile_unfilled_orders(trading_date)

    def update_target_quantities(self, quantities, target_date, reason, version=None,
                                 bypass_pause=False, signal_date=None, bypass_replanning=False):
        bridge = self.orchestration
        if not bridge.installing and (bridge.started or bridge.engine.lifecycle != "initializing"):
            raise RuntimeError("target update requires the active orchestration task")
        result = super().update_target_quantities(
            quantities, target_date, reason, version, bypass_pause, signal_date, bypass_replanning)
        plan = self.trading_state.planned_models.get(self._target_version)
        if plan is not None:
            self.trading_state.formal_model = plan
        return result

    def submit_order(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation()
        return super().submit_order(*args, **kwargs)

    def _submit_order_quantity(self, trading_date, intent, quantity, reason,
                               slice_index=None, slice_count=None):
        bridge = self.orchestration
        if bridge.engine.owner_task() == "exposure_correction":
            quantity = bridge.exposure_admission.quantity(
                bridge.snapshot_provider.capture(), str(intent.instrument_id),
                intent.side.name.lower(), quantity)
            if quantity <= 0:
                intent.context.record(skip_reason="exposure_admission_blocked")
                return False
        return super()._submit_order_quantity(trading_date, intent, quantity, reason,
                                             slice_index, slice_count)

    def submit_order_list(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation()
        return super().submit_order_list(*args, **kwargs)

    def modify_order(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation("modify")
        return super().modify_order(*args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation("cancel")
        return super().cancel_order(*args, **kwargs)

    def cancel_orders(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation("cancel")
        return super().cancel_orders(*args, **kwargs)

    def cancel_all_orders(self, *args, **kwargs):
        self.orchestration.reject_unowned_operation("cancel")
        return super().cancel_all_orders(*args, **kwargs)

