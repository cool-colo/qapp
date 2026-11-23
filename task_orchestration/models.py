from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class RunState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING = "waiting"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNACHIEVABLE = "unachievable"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.UNACHIEVABLE,
                      RunState.CANCELLED, RunState.EXPIRED})


@dataclass(frozen=True)
class Decision:
    state: RunState = RunState.RUNNING
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    requires_stable: bool = False

    def __post_init__(self):
        if self.requires_stable and self.state in TERMINAL:
            raise ValueError("a terminal decision cannot request resumption after cleanup")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class Proposal:
    key: str
    payload: Any = None
    expires_ns: int | None = None
    trigger_evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    priority: int
    scope: str = "default"
    enabled: bool = True
    can_preempt: bool = True
    preemptible: bool = True
    interval_ns: int = 1_000_000_000


@dataclass(frozen=True)
class TaskContext:
    now_ns: int
    event_seq: int
    run_id: str
    owner_epoch: int
    proposal: Proposal


@dataclass(frozen=True)
class ActionRecord:
    sequence: int
    now_ns: int
    action: str
    task_id: str
    run_id: str
    reason: str
    evidence: Mapping[str, Any]


@dataclass
class TaskRun:
    run_id: str
    task_id: str
    proposal: Proposal
    enqueue_seq: int
    state: RunState = RunState.QUEUED
    owner_epoch: int = 0
    process: Any = None
    reason: str = ""
    started: bool = False
    phase: str = "queued"
    cleanup_kind: str | None = None
    interrupted: bool = False
    final_decision: Decision | None = None
    cleanup_decision: Decision | None = None
