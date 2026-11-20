from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import threading
from typing import Callable
from copy import deepcopy

from task_orchestration.components.processes.task_process import TaskProcess
from task_orchestration.components.triggers.trigger_policy import TriggerPolicy
from task_orchestration.models import (
    ActionRecord, Decision, Proposal, RunState, TaskContext, TaskRun, TaskSpec, TERMINAL,
)


@dataclass
class _Binding:
    spec: TaskSpec
    factory: Callable[[], TaskProcess]
    trigger: TriggerPolicy | None
    next_ns: int | None = None


class TaskOrchestrator:
    """Serialized, nonblocking arbitration. No runtime persistence or domain imports."""

    def __init__(self, *, audit: Callable[[ActionRecord], None] | None = None):
        self._bindings: dict[str, _Binding] = {}
        self._runs: dict[str, TaskRun] = {}
        self._owners: dict[str, str] = {}
        self._epoch = 0
        self._sequence = 0
        self._run_sequence = 0
        self._now = 0
        self._lock = threading.RLock()
        self._events = deque()
        self._draining = False
        self._audit = audit
        self.records: list[ActionRecord] = []
        self.statistics = Counter()
        self.lifecycle = "initializing"
        self._bootstrap_id: str | None = None
        self.paused = False

    def register(self, spec: TaskSpec, factory: Callable[[], TaskProcess],
                 trigger: TriggerPolicy | None = None, *, bootstrap: bool = False) -> None:
        with self._lock:
            if spec.task_id in self._bindings or spec.interval_ns <= 0:
                raise ValueError("duplicate task ID or non-positive interval")
            if bootstrap and self._bootstrap_id is not None:
                raise ValueError("only one bootstrap task is allowed")
            self._bindings[spec.task_id] = _Binding(spec, factory, trigger)
            if bootstrap:
                self._bootstrap_id = spec.task_id

    def start(self, now_ns: int) -> None:
        with self._lock:
            if self._bootstrap_id is None:
                raise ValueError("bootstrap task is required")
            if self._runs:
                raise RuntimeError("orchestrator already started")
            self._now = now_ns
            self.submit(self._bootstrap_id, Proposal("startup"))
        self.tick(now_ns)

    def submit(self, task_id: str, proposal: Proposal) -> str | None:
        with self._lock:
            binding = self._bindings[task_id]
            if not binding.spec.enabled or self.lifecycle in {"stopping", "stopped"}:
                return None
            # One live instance per definition. The process re-evaluates current facts.
            for run in self._runs.values():
                if run.task_id == task_id and run.state not in TERMINAL:
                    if run.proposal.key != proposal.key:
                        run.proposal = proposal
                        self._record(run, "COALESCED", "new_trigger")
                    return run.run_id
            self._run_sequence += 1
            run = TaskRun(f"run-{self._run_sequence}", task_id, proposal, self._run_sequence)
            self._runs[run.run_id] = run
            self._record(run, "QUEUED", "trigger")
            return run.run_id

    def _context(self, run: TaskRun) -> TaskContext:
        return TaskContext(self._now, self._sequence, run.run_id, run.owner_epoch, run.proposal)

    def _record(self, run: TaskRun, action: str, reason: str, evidence=None) -> None:
        self._sequence += 1
        record = ActionRecord(self._sequence, self._now, action, run.task_id, run.run_id,
                              reason, deepcopy(dict(evidence or {})))
        self.records.append(record)
        self.statistics[action] += 1
        if self._audit is not None:
            try:
                self._audit(record)
            except Exception:
                self.statistics["AUDIT_ERROR"] += 1

    def tick(self, now_ns: int) -> None:
        """Nested callbacks enqueue; they never re-enter a state transition."""
        with self._lock:
            self._events.append(now_ns)
            if self._draining:
                return
            self._draining = True
            try:
                while self._events:
                    value = self._events.popleft()
                    if value < self._now:
                        raise ValueError("logical time must not move backwards")
                    self._now = value
                    self._step()
            finally:
                self._draining = False

    def _step(self) -> None:
        if self.lifecycle == "stopped":
            return
        if self.lifecycle == "ready" and not self.paused:
            # Evaluate all due candidates before selecting a winner.
            for task_id, binding in sorted(self._bindings.items()):
                if not binding.spec.enabled or binding.trigger is None:
                    continue
                if binding.next_ns is None:
                    binding.next_ns = self._now
                if binding.next_ns > self._now:
                    continue
                missed = (self._now - binding.next_ns) // binding.spec.interval_ns
                binding.next_ns += (missed + 1) * binding.spec.interval_ns
                try:
                    proposal = binding.trigger.evaluate(self._now)
                    if proposal is not None:
                        self.submit(task_id, proposal)
                except Exception as exc:
                    self.statistics["TRIGGER_ERROR"] += 1
                    synthetic = TaskRun("", task_id, Proposal("error"), 0)
                    self._record(synthetic, "TRIGGER_ERROR", str(exc))

        for run in list(self._runs.values()):
            if run.state in TERMINAL:
                continue
            if run.proposal.expires_ns is not None and self._now >= run.proposal.expires_ns:
                expired = Decision(RunState.EXPIRED, "proposal_expired",
                                   {"expires_ns": run.proposal.expires_ns})
                if self._owners.get(self._bindings[run.task_id].spec.scope) == run.run_id:
                    self._request_finish(run, expired)
                else:
                    self._finish(run, expired)

        scopes = sorted({binding.spec.scope for binding in self._bindings.values()})
        for scope in scopes:
            owner = self._runs.get(self._owners.get(scope, ""))
            queued = [r for r in self._runs.values()
                      if self._bindings[r.task_id].spec.scope == scope
                      and r.state in {RunState.QUEUED, RunState.SUSPENDED}
                      and (self.lifecycle == "ready" or r.task_id == self._bootstrap_id)]
            queued.sort(key=lambda r: (-self._bindings[r.task_id].spec.priority,
                                      r.enqueue_seq, r.task_id))
            candidate = queued[0] if queued and not self.paused and self.lifecycle != "stopping" else None
            if owner is not None:
                spec = self._bindings[owner.task_id].spec
                preempt = (candidate is not None and spec.preemptible
                           and self._bindings[candidate.task_id].spec.can_preempt
                           and self._bindings[candidate.task_id].spec.priority > spec.priority)
                if owner.final_decision is not None or preempt or self.paused or self.lifecycle == "stopping":
                    if owner.state not in {RunState.SUSPENDING, RunState.STOPPING}:
                        owner.state = RunState.STOPPING if owner.final_decision else RunState.SUSPENDING
                        self._record(owner, "QUIESCE_REQUESTED", "stop" if owner.final_decision else "preempt_or_pause")
                    result = self._cleanup(owner)
                    if result.state == RunState.SUCCEEDED:
                        self._owners.pop(scope, None)
                        if owner.final_decision is not None:
                            self._finish(owner, owner.final_decision)
                        else:
                            owner.state = RunState.SUSPENDED
                            # A new process must reassess current state after resumption.
                            owner.process = None
                            self._record(owner, "SUSPENDED", result.reason)
                        owner = None
                    else:
                        continue

            if owner is None and candidate is not None:
                owner = candidate
                try:
                    owner.process = self._bindings[owner.task_id].factory()
                except Exception as exc:
                    self._finish(owner, Decision(RunState.FAILED, f"component_creation_failed: {exc}"))
                    continue
                self._epoch += 1
                owner.owner_epoch = self._epoch
                owner.state = RunState.RUNNING
                owner.cleanup_decision = None
                self._owners[scope] = owner.run_id
                self._record(owner, "ADMITTED", "priority_and_scope", {"epoch": owner.owner_epoch})
            if owner is not None and not self.paused:
                result = self._call(owner)
                if result.state in TERMINAL:
                    # Completion also requires cleanup before releasing responsibility.
                    final = self._request_finish(owner, result)
                    cleaned = self._cleanup(owner)
                    if cleaned.state == RunState.SUCCEEDED:
                        self._owners.pop(scope, None)
                        self._finish(owner, final)
                    else:
                        owner.state = RunState.STOPPING
                else:
                    if owner.state != result.state or owner.reason != result.reason:
                        self._record(owner, "PROGRESS", result.reason, result.evidence)
                    owner.state, owner.reason = result.state, result.reason
        if self.lifecycle == "stopping" and not self._owners:
            self.lifecycle = "stopped"

    def _call(self, run: TaskRun, *, cleanup=False) -> Decision:
        try:
            return (run.process.quiesce(self._context(run)) if cleanup
                    else run.process.advance(self._context(run)))
        except Exception as exc:
            self._record(run, "COMPONENT_ERROR", str(exc))
            # Never release an owner whose cleanup status is unknown.
            return Decision(RunState.WAITING if cleanup else RunState.FAILED,
                            f"{type(exc).__name__}: {exc}")

    def _request_finish(self, run: TaskRun, result: Decision) -> Decision:
        # Latch the complete outcome before cleanup can mutate component state.
        # A later deadline/control request cannot rewrite an already decided result.
        if run.final_decision is None:
            if result.state not in TERMINAL:
                raise ValueError("final decision must have a terminal state")
            run.final_decision = Decision(result.state, result.reason, deepcopy(dict(result.evidence)))
            self._record(run, "OUTCOME_DECIDED", result.reason,
                         {**run.final_decision.evidence, "state": result.state.value})
        return run.final_decision

    def _cleanup(self, run: TaskRun) -> Decision:
        result = self._call(run, cleanup=True)
        if run.cleanup_decision != result:
            run.cleanup_decision = Decision(result.state, result.reason, deepcopy(dict(result.evidence)))
            action = "CLEANUP_COMPLETED" if result.state == RunState.SUCCEEDED else "WAITING_CLEANUP"
            self._record(run, action, result.reason, {**result.evidence, "state": result.state.value})
        run.reason = result.reason
        return result

    def _finish(self, run: TaskRun, result: Decision) -> None:
        final = self._request_finish(run, result)
        run.state, run.reason = final.state, final.reason
        self._record(run, "TERMINATED", final.reason, {**final.evidence, "state": final.state.value})
        if (run.task_id == self._bootstrap_id and final.state == RunState.SUCCEEDED
                and self.lifecycle == "initializing"):
            self.lifecycle = "ready"

    def set_enabled(self, task_id: str, enabled: bool) -> None:
        from dataclasses import replace
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._lock:
            binding = self._bindings[task_id]
            if task_id == self._bootstrap_id:
                raise ValueError("bootstrap cannot be disabled")
            binding.spec = replace(binding.spec, enabled=enabled)
            binding.next_ns = None
            if not enabled:
                for run in self._runs.values():
                    if run.task_id == task_id and run.state in {RunState.QUEUED, RunState.SUSPENDED}:
                        self._finish(run, Decision(RunState.CANCELLED, "definition_disabled"))

    def reevaluate(self) -> None:
        with self._lock:
            for binding in self._bindings.values():
                binding.next_ns = None

    def cancel(self, run_id: str) -> None:
        with self._lock:
            run = self._runs[run_id]
            if run.state in TERMINAL:
                return
            if run.run_id in self._owners.values():
                self._request_finish(run, Decision(RunState.CANCELLED, "cancelled"))
            else:
                self._finish(run, Decision(RunState.CANCELLED, "cancelled"))

    def stop(self) -> None:
        with self._lock:
            self.lifecycle = "stopping"
            for run in self._runs.values():
                if run.state not in TERMINAL:
                    if run.run_id in self._owners.values():
                        self._request_finish(run, Decision(RunState.CANCELLED, "shutdown"))
                    else:
                        self._finish(run, Decision(RunState.CANCELLED, "shutdown"))

    def owns(self, context: TaskContext, scope: str = "default") -> bool:
        with self._lock:
            return (self._owners.get(scope) == context.run_id
                    and self._runs[context.run_id].owner_epoch == context.owner_epoch)

    def owner_task(self, scope: str = "default") -> str | None:
        with self._lock:
            run_id = self._owners.get(scope)
            return None if run_id is None else self._runs[run_id].task_id

    def snapshot(self) -> dict:
        with self._lock:
            return {"lifecycle": self.lifecycle, "paused": self.paused,
                    "revision": self._sequence, "owners": dict(self._owners),
                    "tasks": {k: {"enabled": b.spec.enabled, "priority": b.spec.priority,
                                  "next_evaluation_ns": b.next_ns} for k, b in self._bindings.items()},
                    "runs": [{"run_id": r.run_id, "task_id": r.task_id, "state": r.state.value,
                              "reason": r.reason, "epoch": r.owner_epoch,
                              "final_result": self._decision_snapshot(r.final_decision),
                              "cleanup_result": self._decision_snapshot(r.cleanup_decision)}
                             for r in self._runs.values()],
                    "statistics": dict(self.statistics)}

    @staticmethod
    def _decision_snapshot(result: Decision | None) -> dict | None:
        if result is None:
            return None
        return {"state": result.state.value, "reason": result.reason,
                "evidence": deepcopy(dict(result.evidence))}

    def actions(self, *, after_sequence: int = 0, limit: int = 100) -> list[dict]:
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid action query range")
        with self._lock:
            return [{"sequence": r.sequence, "time_ns": r.now_ns, "action": r.action,
                     "task_id": r.task_id, "run_id": r.run_id, "reason": r.reason,
                     "evidence": deepcopy(r.evidence)}
                    for r in self.records if r.sequence > after_sequence][:limit]

