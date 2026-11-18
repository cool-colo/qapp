from __future__ import annotations

import asyncio
import dataclasses
from datetime import date
from decimal import Decimal
from enum import Enum
from collections.abc import Mapping
import threading
import time
from datetime import timedelta
from typing import Any
from uuid import uuid4

from strategies.model_target_planners.base import ModelTargetPlan
from strategies.replan_triggers import ExecutionWindowReplanValidity, ReplanValidity


class ReplanningBusyError(RuntimeError):
    pass


def planning_json(value: Any) -> Any:
    """Serialize immutable planning inputs without exposing infrastructure config."""
    if dataclasses.is_dataclass(value):
        return {field.name: planning_json(value.__dict__[field.name]) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): planning_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [planning_json(item) for item in value]
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclasses.dataclass
class PlanningAttempt:
    request_id: str
    trading_date: date
    source: str
    parent_version: str | None
    status: str = "pending"
    triggers: list[Any] = dataclasses.field(default_factory=list)
    follow_up_triggers: list[Any] = dataclasses.field(default_factory=list)
    inputs: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    validity: ReplanValidity = dataclasses.field(default_factory=ExecutionWindowReplanValidity)

    def payload(self, plan: ModelTargetPlan | None = None) -> dict[str, Any]:
        return {**planning_json(self), "plan": planning_json(plan)}


class ReplanningCoordinator:
    """Coordinate orders and persistence; all business decisions stay on the host."""

    def __init__(self, host) -> None:
        self.host = host
        self.auto_enabled = bool(host.config.auto_replan_enabled)
        self._lock = threading.Lock()
        self.current: PlanningAttempt | None = None
        self.initial_in_progress = False
        self.last: dict[str, Any] | None = None
        self.counts: dict[str, int] = {"automatic": 0, "manual": 0}
        self.day: date | None = None
        self.task = None
        self._coroutine_started = False
        self.stopped = False
        self._canceled = set()
        self._ready_at = 0
        self._deadline = 0

    @property
    def busy(self) -> bool:
        return self.current is not None or self.initial_in_progress

    def set_auto_enabled(self, enabled: bool) -> None:
        if enabled and not self.auto_enabled:
            self.host._replan_triggers.reset()
        self.auto_enabled = enabled

    def request(self, source: str, triggers=(), *, validity: ReplanValidity | None = None) -> str:
        host = self.host
        validity = ExecutionWindowReplanValidity() if validity is None else validity
        if source not in ("manual", "automatic"):
            raise ValueError(f"unknown replanning source: {source}")
        if self.stopped:
            raise ValueError("strategy is stopped")
        if not host._async_scheduler.has_running_loop and host._execution_state_reconciler.is_configured:
            raise RuntimeError("live replanning requires the running node event loop")
        with host._trading_action_lock, self._lock:
            day = host._clock_date()
            if not validity.allows(host, day, starting=True):
                raise ValueError("replanning requires its permitted planning window (execution and cancellation window for ordinary requests)")
            if source == "automatic" and not host.automatic_replanning_allowed():
                raise ValueError("automatic replanning is disabled for the current target or paused")
            if self.busy:
                if source == "automatic" and validity.coalesce_while_busy and self.current is not None:
                    destination = (self.current.triggers
                                   if self.current.status in ("pending", "canceling", "reconciling")
                                   else self.current.follow_up_triggers)
                    destination.extend(trigger for trigger in triggers if trigger not in destination)
                raise ReplanningBusyError("replanning is already in progress")
            if self.day != day:
                self.day, self.counts = day, {"automatic": 0, "manual": 0}
            attempt = PlanningAttempt(uuid4().hex, day, source, host._target_version or None,
                                      triggers=list(triggers), validity=validity)
            self.current = attempt
            self._coroutine_started = False
            host._replanning_blocked = True
            self.counts[source] += 1
        # Scheduling must happen after releasing the lock: synchronous backtests
        # resolve the coroutine immediately.
        try:
            if host._async_scheduler.has_running_loop:
                self.task = host._async_scheduler.schedule(self._run(attempt))
                # Canceling an asyncio task before coroutine entry skips its
                # finally block. Completion must finalize that request as well.
                self.task.add_done_callback(
                    lambda future: self._cancel_completed(attempt) if future.cancelled() else None)
            else:
                # A backtest must return to its engine to deliver cancellation and
                # fill events. Its clock advances this same orchestration in steps.
                host.archive_planning_attempt(attempt)
                self._deadline = host.clock.timestamp_ns() + int(host._execution_state_reconciler.timeout_secs * 1e9)
                self._canceled = set()
                self._ready_at = 0
                self._schedule_clock_step()
        except BaseException:
            self.current = None
            host._replanning_blocked = False
            raise
        return attempt.request_id

    def _schedule_clock_step(self) -> None:
        host = self.host
        host.clock.set_time_alert(name="MODEL-REPLAN-STEP",
                                  alert_time=host.clock.utc_now() + timedelta(milliseconds=50),
                                  callback=self._clock_step, override=True)

    def _clock_step(self, _event) -> None:
        attempt, host = self.current, self.host
        if attempt is None or self.stopped:
            return
        plan = None
        try:
            if host.clock.timestamp_ns() >= self._deadline:
                raise TimeoutError("replanning timed out waiting for terminal orders")
            self._validate_window(attempt, canceling=True)
            orders = host.replanning_orders()
            if orders:
                self._require_cancellation_window()
                attempt.status = "canceling"
                self._ready_at = 0
                for order in orders:
                    key = str(order.client_order_id)
                    if key not in self._canceled and not order.is_pending_cancel:
                        host.cancel_for_replanning(order)
                        self._canceled.add(key)
                self._schedule_clock_step()
                return
            if not self._ready_at:
                self._ready_at = host.clock.timestamp_ns() + max(
                    1, int(float(host.config.cancel_reconverge_delay_secs) * 1e9))
                self._settled_activity = host._execution_activity
                self._schedule_clock_step()
                return
            if self._settled_activity != host._execution_activity:
                self._ready_at = 0
                self._schedule_clock_step()
                return
            if host.clock.timestamp_ns() < self._ready_at:
                self._schedule_clock_step()
                return
            context, request, forced_exits, degraded = self._capture_inputs(attempt)
            host.archive_planning_attempt(attempt)
            candidate, error = None, None
            if degraded is None:
                try:
                    candidate = host._target_planner.plan(request)
                except Exception as exc:
                    error = exc
            attempt.inputs["optimizer_result"] = planning_json(candidate)
            plan = host.finish_target_planning(request, forced_exits, context, candidate,
                                               error=error, degraded_reason=degraded)
            plan = self._apply_result(attempt, plan, error, degraded)
        except Exception as exc:
            attempt.status, attempt.error = "failed", str(exc)
            host.log.error(f"replanning failed: {exc}")
            try:
                host.archive_planning_attempt(attempt)
            except Exception as archive_error:
                host.log.error(f"could not archive failed replanning: {archive_error}")
        finally:
            if attempt.status in ("committed", "safety", "keep_current", "failed"):
                self._finish(attempt, plan)

    def _apply_result(self, attempt, plan, error=None, degraded=None) -> ModelTargetPlan | None:
        host = self.host
        with host._trading_action_lock:
            self._ensure_active(attempt)
            self._validate_window(attempt)
            if host._execution_activity != self._captured_activity or host.replanning_orders():
                raise RuntimeError("execution state changed while the planner was running")
            if plan is None:
                attempt.status = "keep_current"
                attempt.error = host._last_planning_error
                host.archive_planning_attempt(attempt)
            else:
                attempt.inputs["planner_request_id"] = plan.request_id
                plan = dataclasses.replace(plan, request_id=attempt.request_id,
                                           parent_version=attempt.parent_version)
                attempt.status = "committed" if plan.persistable else "safety"
                host.archive_planning_attempt(attempt, plan)
                # The request was already authorized. Installing its result must not
                # clear or depend on independently changed user pause flags.
                host.accept_model_target_plan(plan, bypass_pause=True)
        return plan

    def _ensure_active(self, attempt: PlanningAttempt) -> None:
        if self.stopped or self.current is not attempt:
            raise asyncio.CancelledError

    def _validate_window(self, attempt: PlanningAttempt, *, canceling: bool = False) -> None:
        if not attempt.validity.allows(self.host, attempt.trading_date, canceling=canceling):
            raise RuntimeError("permitted planning window closed during replanning or trading date changed")

    def _require_cancellation_window(self) -> None:
        if not self.host._within_cancel_window():
            raise RuntimeError("cancellation window closed during replanning with active orders")

    def _capture_inputs(self, attempt: PlanningAttempt):
        host = self.host
        with self._lock:
            self._ensure_active(attempt)
            # Capture is the cutoff: later triggers cannot modify these inputs.
            # Queue them for rechecking after this attempt, in both runtimes.
            attempt.status = "planning"
        self._validate_window(attempt)
        self._captured_activity = host._execution_activity
        context = host.build_planning_context(attempt.trading_date, stage="continuous")
        request, forced_exits, degraded = host.prepare_target_planning(attempt.trading_date, context)
        attempt.inputs = host.capture_planning_inputs(request, forced_exits, context)
        return context, request, forced_exits, degraded

    def interrupt(self, attempt: PlanningAttempt) -> None:
        host = self.host
        with host._trading_action_lock:
            with self._lock:
                if self.current is not attempt:
                    return
                attempt.status, attempt.error = "interrupted", "strategy stopped"
            try:
                host.archive_planning_attempt(attempt)
            except Exception as exc:
                host.log.error(f"could not archive interrupted replanning: {exc}")
            finally:
                self._finish(attempt, None)

    def _cancel_completed(self, attempt: PlanningAttempt) -> None:
        host = self.host
        with host._trading_action_lock:
            with self._lock:
                if self.current is not attempt or self._coroutine_started:
                    return
            # A cross-thread future reports cancellation before its coroutine has
            # actually stopped. Entered coroutines retain the gate until finally;
            # only an unstarted request needs independent completion cleanup.
            self.interrupt(attempt)

    def _finish(self, attempt, plan) -> None:
        host = self.host
        with host._trading_action_lock, self._lock:
            if self.current is not attempt:
                return
            self.last = attempt.payload(plan)
            host._replan_triggers.rearm(attempt.follow_up_triggers)
            self.current = None
            self._coroutine_started = False
            host._replanning_blocked = False
        host.run_pending_replanning()
        if host._replanning_blocked:
            return
        if not self.stopped and host._within_execution_window() and not host._trading_paused:
            host._schedule_convergence(current_date=host._clock_date(), trigger=host.replan_resume_trigger())

    async def _run(self, attempt: PlanningAttempt) -> None:
        host = self.host
        plan = None
        try:
            with host._trading_action_lock, self._lock:
                self._ensure_active(attempt)
                self._coroutine_started = True
            host.archive_planning_attempt(attempt)
            attempt.status = "canceling"
            host.archive_planning_attempt(attempt)
            deadline = time.monotonic() + host._execution_state_reconciler.timeout_secs
            canceled = set()
            while True:
                self._ensure_active(attempt)
                self._validate_window(attempt, canceling=True)
                # No lock is held across an await or an external request.
                acquired = host._converge_lock.acquire(blocking=False)
                if acquired:
                    try:
                        orders = host.replanning_orders()
                        if orders:
                            self._require_cancellation_window()
                        for order in orders:
                            key = str(order.client_order_id)
                            if key not in canceled and not order.is_pending_cancel:
                                host.cancel_for_replanning(order)
                                canceled.add(key)
                    finally:
                        host._converge_lock.release()
                    if not orders:
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError("replanning timed out waiting for terminal orders")
                await asyncio.sleep(0.05)
            attempt.status = "reconciling"
            host.archive_planning_attempt(attempt)
            await asyncio.sleep(float(host.config.cancel_reconverge_delay_secs))
            while True:
                self._ensure_active(attempt)
                self._validate_window(attempt)
                activity = host._execution_activity
                await host._execution_state_reconciler.reconcile_once()
                await asyncio.sleep(float(host.config.cancel_reconverge_delay_secs))
                orders = host.replanning_orders()
                if orders:
                    self._require_cancellation_window()
                if not orders and activity == host._execution_activity:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("execution state did not settle for replanning")
            self._validate_window(attempt)
            context, request, forced_exits, degraded_reason = self._capture_inputs(attempt)
            host.archive_planning_attempt(attempt)
            candidate = None
            error = None
            if degraded_reason is None:
                try:
                    candidate = await asyncio.to_thread(host._target_planner.plan, request)
                except Exception as exc:
                    error = exc
            attempt.inputs["optimizer_result"] = planning_json(candidate)
            plan = host.finish_target_planning(request, forced_exits, context, candidate,
                                               error=error, degraded_reason=degraded_reason)
            if plan is None:
                plan = self._apply_result(attempt, plan, error, degraded_reason)
            else:
                if host._execution_activity != activity or host.replanning_orders():
                    raise RuntimeError("execution state changed while the planner was running")
                plan = self._apply_result(attempt, plan)
        except asyncio.CancelledError:
            self.interrupt(attempt)
            raise
        except Exception as exc:
            attempt.status, attempt.error = "failed", str(exc)
            host.log.error(f"replanning failed: request_id={attempt.request_id} error={exc}")
            try:
                host.archive_planning_attempt(attempt)
            except Exception as archive_error:
                host.log.error(f"could not archive failed replanning: {archive_error}")
        finally:
            self._finish(attempt, plan)

    def state(self) -> dict[str, Any]:
        return {"auto_replan_enabled": self.auto_enabled, "replanning": self.busy,
                "planning": self.current.payload() if self.current else None,
                "last_planning": self.last, "replan_date": str(self.day) if self.day else None,
                "replan_counts": dict(self.counts),
                **self.host.replanning_trigger_state()}
