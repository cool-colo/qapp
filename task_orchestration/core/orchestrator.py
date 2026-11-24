from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import threading
from typing import Callable
from copy import deepcopy

from task_orchestration.components.processes.task_process import TaskProcess
from task_orchestration.components.stabilizers.state_stabilizer import StateStabilizer
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

    def __init__(self, *, stabilizer_factory: Callable[[str], StateStabilizer],
                 audit: Callable[[ActionRecord], None] | None = None):
        self._stabilizer_factory = stabilizer_factory
        self._stabilizers: dict[str, StateStabilizer] = {}
        self._callback_phase: str | None = None
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
            if bootstrap and (spec.skip_entry_cleanup or spec.skip_exit_cleanup):
                raise ValueError("bootstrap cannot skip cleanup")
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
            self._record(run, "QUEUED", "trigger", proposal.trigger_evidence)
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

    def tick(self, now_ns: int, *, ingress: Callable[[], None] | None = None, evaluate_triggers: bool = True) -> None:
        """Nested callbacks enqueue; they never re-enter a state transition."""
        with self._lock:
            # Repeated wakeups for the same instant need only one next cycle.
            if self._events and self._events[-1] == (now_ns, ingress, evaluate_triggers):
                return
            self._events.append((now_ns, ingress, evaluate_triggers))
            if self._draining:
                return
            self._draining = True
            try:
                while self._events:
                    value, receive, evaluate = self._events.popleft()
                    if value < self._now:
                        raise ValueError("logical time must not move backwards")
                    self._now = value
                    if receive is not None:
                        receive()
                    self._step(evaluate)
            finally:
                self._draining = False

    def _step(self, evaluate_triggers=True) -> None:
        if self.lifecycle == "stopped":
            return
        if evaluate_triggers and self.lifecycle == "ready" and not self.paused:
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
            self._step_scope(scope)
        if self.lifecycle == "stopping" and not self._owners:
            self.lifecycle = "stopped"

    def _candidate(self, scope: str) -> TaskRun | None:
        if self.paused or self.lifecycle in {"stopping", "stopped"}:
            return None
        queued = [r for r in self._runs.values()
                  if self._bindings[r.task_id].spec.scope == scope
                  and self._bindings[r.task_id].spec.enabled
                  and r.state in {RunState.QUEUED, RunState.SUSPENDED}
                  and (r.proposal.expires_ns is None or self._now < r.proposal.expires_ns)
                  and (self.lifecycle == "ready" or r.task_id == self._bootstrap_id)]
        return min(queued, key=lambda r: (-self._bindings[r.task_id].spec.priority,
                                         r.enqueue_seq, r.task_id), default=None)

    def _preempts(self, candidate: TaskRun | None, owner: TaskRun) -> bool:
        old = self._bindings[owner.task_id].spec
        return (candidate is not None and old.preemptible
                and self._bindings[candidate.task_id].spec.can_preempt
                and self._bindings[candidate.task_id].spec.priority > old.priority)

    def _step_scope(self, scope: str) -> None:
        owner = self._runs.get(self._owners.get(scope, ""))
        candidate = self._candidate(scope)
        if owner is None:
            if candidate is None:
                return
            owner = candidate
            self._owners[scope] = owner.run_id
            self._begin_cleanup(owner, "entry", "before_task_start")
        elif (owner.final_decision is not None or self.paused or self.lifecycle == "stopping"
              or self._preempts(candidate, owner)):
            if (self.paused or self.lifecycle == "stopping"
                    or (owner.final_decision is None and self._preempts(candidate, owner))):
                owner.cleanup_forced = True
            if owner.phase == "executing":
                self._begin_cleanup(owner, "exit",
                                    owner.final_decision.reason if owner.final_decision else
                                    "pause" if self.paused else "higher_priority", candidate)
            elif owner.cleanup_kind == "reenter":
                owner.cleanup_kind = "exit"
                self._record(owner, "CLEANUP_REDIRECTED", "exit_requested_during_cleanup",
                             {"candidate_run_id": None if candidate is None else candidate.run_id,
                              "final_reason": None if owner.final_decision is None else owner.final_decision.reason})

        while owner.phase in {"preparing", "cleaning"}:
            result = self._cleanup(owner)
            if result.state != RunState.SUCCEEDED:
                return
            kind = owner.cleanup_kind
            cleaned_run_id = owner.run_id
            owner.cleanup_kind = None
            self._stabilizers.pop(scope, None)
            if (kind == "reenter" and owner.final_decision is None and not self.paused
                    and self.lifecycle != "stopping"):
                self._epoch += 1
                owner.owner_epoch = self._epoch
                owner.cleanup_forced = False
                owner.phase, owner.state = "executing", RunState.RUNNING
                self._record(owner, "RESUMED_CLEAN", "stable_state_requested",
                             {"epoch": owner.owner_epoch})
            else:
                self._owners.pop(scope)
                if owner.final_decision is not None:
                    self._finish(owner, owner.final_decision)
                elif kind == "entry":
                    owner.cleanup_forced = False
                    owner.phase, owner.state = "queued", RunState.QUEUED
                elif not self._bindings[owner.task_id].spec.enabled:
                    self._finish(owner, Decision(RunState.CANCELLED, "definition_disabled"))
                else:
                    owner.cleanup_forced = False
                    owner.phase, owner.state = "suspended", RunState.SUSPENDED
                    owner.process = None
                    self._record(owner, "SUSPENDED", result.reason)
                # Re-arbitrate using current enablement, deadlines and priorities.
                candidate = self._candidate(scope)
                if candidate is None:
                    return
                owner = candidate
                # Only real cleanup establishes a stable scope for a different
                # winner. A skipped boundary cannot authorize its admission.
                skipped = result.evidence.get("cleanup_skipped", False)
                if skipped and (kind != "entry" or candidate.run_id != cleaned_run_id):
                    self._owners[scope] = owner.run_id
                    self._begin_cleanup(owner, "entry", "before_task_start")
                    continue
                if not self._admit(owner, scope, stable_scope=not skipped,
                                   cleanup_result=result if kind == "entry" else None):
                    return

        if owner.phase != "executing" or self.paused:
            return
        result = self._call(owner)
        if result.state in TERMINAL:
            final = self._request_finish(owner, result)
            self._begin_cleanup(owner, "exit", "business_outcome")
            if self._cleanup(owner).state == RunState.SUCCEEDED:
                self._owners.pop(scope)
                self._stabilizers.pop(scope, None)
                self._finish(owner, final)
        elif result.requires_stable:
            self._record(owner, "STABLE_STATE_REQUESTED", result.reason, result.evidence)
            self._begin_cleanup(owner, "reenter", result.reason)
        else:
            if owner.state != result.state or owner.reason != result.reason:
                self._record(owner, "PROGRESS", result.reason, result.evidence)
            owner.state, owner.reason = result.state, result.reason

    def _admit(self, run: TaskRun, scope: str, *, stable_scope: bool = True,
               cleanup_result: Decision | None = None) -> bool:
        try:
            run.process = self._bindings[run.task_id].factory()
        except Exception as exc:
            self._owners[scope] = run.run_id
            self._request_finish(run, Decision(RunState.FAILED, f"component_creation_failed: {exc}"))
            self._begin_cleanup(run, "exit", "component_creation_failed")
            return False
        self._epoch += 1
        run.owner_epoch = self._epoch
        run.state, run.phase = RunState.RUNNING, "executing"
        run.started = True
        run.cleanup_decision = cleanup_result
        run.cleanup_forced = False
        self._owners[scope] = run.run_id
        self._record(run, "ADMITTED", "priority_and_stable_scope" if stable_scope else
                     "priority_and_cleanup_policy", {"epoch": run.owner_epoch, "stable_scope": stable_scope})
        return True

    def _begin_cleanup(self, run: TaskRun, kind: str, reason: str,
                       candidate: TaskRun | None = None) -> None:
        run.cleanup_kind = kind
        run.cleanup_forced = run.cleanup_forced or kind == "reenter"
        run.interrupted = False
        run.cleanup_decision = None
        run.phase = "preparing" if kind == "entry" else "cleaning"
        run.state = (RunState.PREPARING if kind == "entry" else
                     RunState.STOPPING if run.final_decision else RunState.SUSPENDING)
        self._record(run, "CLEANUP_REQUESTED", reason, {
            "kind": kind, "epoch": run.owner_epoch,
            "candidate_run_id": None if candidate is None else candidate.run_id,
            "candidate_task_id": None if candidate is None else candidate.task_id})

    def _call(self, run: TaskRun) -> Decision:
        self._callback_phase = "execute"
        try:
            return run.process.advance(self._context(run))
        except Exception as exc:
            self._record(run, "COMPONENT_ERROR", str(exc))
            return Decision(RunState.FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            self._callback_phase = None

    def operation_phase(self) -> str | None:
        """Capability is valid only during a framework-invoked component call."""
        with self._lock:
            return self._callback_phase

    def _request_finish(self, run: TaskRun, result: Decision) -> Decision:
        # Latch the complete outcome before cleanup can mutate component state.
        # A later deadline/control request cannot rewrite an already decided result.
        run.cleanup_forced = run.cleanup_forced or result.state != RunState.SUCCEEDED
        if run.final_decision is None:
            if result.state not in TERMINAL:
                raise ValueError("final decision must have a terminal state")
            run.final_decision = Decision(result.state, result.reason, deepcopy(dict(result.evidence)))
            if run.phase == "cleaning":
                run.state = RunState.STOPPING
            self._record(run, "OUTCOME_DECIDED", result.reason,
                         {**run.final_decision.evidence, "state": result.state.value})
        return run.final_decision

    def _can_skip_cleanup(self, run: TaskRun) -> bool:
        if run.cleanup_forced or self.paused or self.lifecycle in {"stopping", "stopped"}:
            return False
        if run.proposal.expires_ns is not None and self._now >= run.proposal.expires_ns:
            return False
        spec = self._bindings[run.task_id].spec
        if run.cleanup_kind == "entry":
            return spec.skip_entry_cleanup and run.final_decision is None and spec.enabled
        return (run.cleanup_kind == "exit" and spec.skip_exit_cleanup
                and run.final_decision is not None and run.final_decision.state == RunState.SUCCEEDED)

    def _cleanup(self, run: TaskRun) -> Decision:
        scope = self._bindings[run.task_id].spec.scope
        try:
            if run.process is not None and not run.interrupted:
                self._callback_phase = "interrupt"
                run.process.interrupt(self._context(run))
                run.interrupted = True
            if self.paused or self.lifecycle in {"stopping", "stopped"}:
                run.cleanup_forced = True
            if self._can_skip_cleanup(run) and run.process is not None:
                self._callback_phase = "release"
                run.process.after_cleanup_skipped(self._context(run))
            # Lifecycle hooks can request a forced exit. Recheck the policy before
            # releasing responsibility, even when the business result is latched.
            if self.paused or self.lifecycle in {"stopping", "stopped"}:
                run.cleanup_forced = True
            if self._can_skip_cleanup(run):
                spec = self._bindings[run.task_id].spec
                result = Decision(RunState.SUCCEEDED, "cleanup_skipped_by_task_policy", {
                    "cleanup_skipped": True, "kind": run.cleanup_kind,
                    "epoch": run.owner_epoch,
                    "skip_entry_cleanup": spec.skip_entry_cleanup,
                    "skip_exit_cleanup": spec.skip_exit_cleanup})
            else:
                if scope not in self._stabilizers:
                    self._stabilizers[scope] = self._stabilizer_factory(scope)
                self._callback_phase = "cleanup"
                result = self._stabilizers[scope].stabilize(self._context(run))
                if result.state == RunState.SUCCEEDED and run.process is not None:
                    self._callback_phase = "release"
                    run.process.after_stable(self._context(run))
        except Exception as exc:
            run.cleanup_forced = True
            self._record(run, "COMPONENT_ERROR", str(exc), {"phase": self._callback_phase})
            result = Decision(RunState.WAITING, f"{type(exc).__name__}: {exc}")
        finally:
            self._callback_phase = None
        if run.cleanup_decision != result:
            run.cleanup_decision = Decision(result.state, result.reason, deepcopy(dict(result.evidence)))
            action = ("CLEANUP_SKIPPED" if result.evidence.get("cleanup_skipped", False) else
                      "CLEANUP_COMPLETED" if result.state == RunState.SUCCEEDED else "WAITING_CLEANUP")
            self._record(run, action, result.reason,
                         {**result.evidence, "state": result.state.value, "kind": run.cleanup_kind})
        if result.evidence.get("cleanup_skipped", False) and not self._can_skip_cleanup(run):
            # A serialized control request received by the audit callback is a
            # new forced boundary, even if the earlier skip already succeeded.
            run.cleanup_forced = True
            self._record(run, "CLEANUP_REDIRECTED", "forced_cleanup_after_skip",
                         {"kind": run.cleanup_kind})
            return self._cleanup(run)
        run.reason = result.reason
        return result

    def _finish(self, run: TaskRun, result: Decision) -> None:
        final = self._request_finish(run, result)
        run.state, run.reason, run.phase = final.state, final.reason, "terminal"
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
                    elif run.task_id == task_id and run.phase == "preparing":
                        self._request_finish(run, Decision(RunState.CANCELLED, "definition_disabled"))

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
                    and self._runs[context.run_id].owner_epoch == context.owner_epoch
                    and self._runs[context.run_id].phase == "executing")

    def owner_task(self, scope: str = "default") -> str | None:
        with self._lock:
            run_id = self._owners.get(scope)
            return None if run_id is None else self._runs[run_id].task_id

    def snapshot(self) -> dict:
        with self._lock:
            return {"lifecycle": self.lifecycle, "paused": self.paused, "time_ns": self._now,
                    "revision": self._sequence, "owners": dict(self._owners),
                    "tasks": {k: {"enabled": b.spec.enabled, "priority": b.spec.priority,
                                  "skip_entry_cleanup": b.spec.skip_entry_cleanup,
                                  "skip_exit_cleanup": b.spec.skip_exit_cleanup,
                                  "next_evaluation_ns": b.next_ns} for k, b in self._bindings.items()},
                    "runs": [{"run_id": r.run_id, "task_id": r.task_id, "state": r.state.value,
                              "reason": r.reason, "epoch": r.owner_epoch, "started": r.started,
                              "phase": r.phase, "cleanup_kind": r.cleanup_kind,
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

