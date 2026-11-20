from __future__ import annotations

import asyncio
from dataclasses import replace
from concurrent.futures import Future

from task_orchestration.models import Decision, RunState
from strategies.replanning import PlanningAttempt, planning_json


class RiskManagerGoalPlanner:
    """Reuse existing request/validation rules, isolating optimizer I/O from state."""

    def __init__(self, bridge, *, source="automatic", planning_context=None):
        self.bridge = bridge
        self.source = source
        self.planning_context = planning_context
        self.future = None
        self.attempt = None
        self.finished = False
        self.invalidated = False

    def plan(self, context):
        host = self.bridge.host
        if self.finished:
            return Decision(RunState.SUCCEEDED, "planning_finished")
        if self.future is None:
            day = host._clock_date()
            self.activity = host._execution_activity
            self.parent = host._target_version or None
            self.day = day
            self.epoch = context.owner_epoch
            self.deadline = context.now_ns + int(float(host.config.risk_manager_timeout_secs) * 1e9)
            snapshot = self.planning_context or host.build_planning_context(day, stage="continuous")
            self.snapshot = snapshot
            self.request, self.exits, self.degraded = host.prepare_target_planning(day, snapshot)
            inputs = host.capture_planning_inputs(self.request, self.exits, snapshot)
            request_id = f"orchestration-{context.now_ns}-{context.run_id}"
            self.attempt = PlanningAttempt(request_id, day, self.source, self.parent,
                                          status="planning", inputs=inputs)
            host.archive_planning_attempt(self.attempt)
            host._latest_planning_inputs = inputs
            if self.degraded is not None:
                future = Future()
                future.set_result(None)
                self.future = future
            elif host._async_scheduler.has_running_loop:
                # Only this independent request DTO and planner enter the worker.
                self.future = host._async_scheduler.schedule(
                    asyncio.to_thread(host._target_planner.plan, self.request))
                return Decision(RunState.WAITING, "optimizer_pending")
            else:
                future = Future()
                try:
                    future.set_result(host._target_planner.plan(self.request))
                except Exception as exc:
                    future.set_exception(exc)
                self.future = future
        if self.invalidated or context.owner_epoch != self.epoch:
            return Decision(RunState.FAILED, "planning_authorization_expired")
        if context.now_ns >= self.deadline:
            self.abort("optimizer_timeout")
            return Decision(RunState.FAILED, "optimizer_timeout")
        if not self.future.done():
            return Decision(RunState.WAITING, "optimizer_pending")
        if (host._clock_date() != self.day or host._execution_activity != self.activity
                or (host._target_version or None) != self.parent or host.replanning_orders()):
            self.abort("planning_snapshot_stale")
            return Decision(RunState.FAILED, "planning_snapshot_stale")
        candidate, error = None, None
        try:
            candidate = self.future.result()
        except Exception as exc:
            error = exc
        self.attempt.inputs["optimizer_result"] = planning_json(candidate)
        plan = host.finish_target_planning(self.request, self.exits, self.snapshot, candidate,
                                          error=error, degraded_reason=self.degraded)
        if plan is None:
            self.attempt.status = "keep_current"
            self.attempt.error = host._last_planning_error
            host.archive_planning_attempt(self.attempt)
            self.finished = True
            return Decision(RunState.FAILED, self.attempt.error or "no_feasible_plan")
        plan = replace(plan, request_id=self.attempt.request_id, parent_version=self.parent)
        self.attempt.status = "committed" if plan.persistable else "safety"
        # Existing infrastructure owns the transaction, core never touches a DB.
        host.archive_planning_attempt(self.attempt, plan)
        if host._execution_activity != self.activity or (host._target_version or None) != self.parent:
            return Decision(RunState.FAILED, "state_changed_during_persistence")
        self.bridge.install_model(plan)
        self.finished = True
        return Decision(RunState.SUCCEEDED, "formal_target_committed",
                        {"version": host._target_version, "persistable": plan.persistable})

    def abort(self, reason="planning_interrupted"):
        if self.invalidated or self.finished:
            return
        self.invalidated = True
        if self.attempt is not None:
            self.attempt.status, self.attempt.error = "interrupted", reason
            self.bridge.host.archive_planning_attempt(self.attempt)

