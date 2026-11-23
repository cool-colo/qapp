"""Presentation of published scheduler facts; never evaluates trading conditions."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo


TERMINAL_STATES = {"succeeded", "failed", "unachievable", "cancelled", "expired"}


def build_scheduler_report(state: dict, actions: list[dict], *, history_limit: int = 100) -> dict:
    if not state.get("enabled", False):
        return {"enabled": False}
    configuration = state["configuration"]
    zone = ZoneInfo(configuration["timezone"])

    def local_time(value):
        if value is None or int(value) <= 0:
            return None
        seconds, ns = divmod(int(value), 1_000_000_000)
        return datetime.fromtimestamp(seconds, timezone.utc).replace(
            microsecond=ns // 1000).astimezone(zone).isoformat(timespec="milliseconds")

    revision = int(state["revision"])
    # State and action endpoints can be read across a publication. Ignore newer
    # actions so all visible facts refer to the captured state revision.
    actions = sorted((a for a in actions if a["sequence"] <= revision), key=lambda a: a["sequence"])
    times = {}
    for action in actions:
        entry = times.setdefault(action["run_id"], {})
        if action["action"] == "QUEUED":
            entry.setdefault("queued_at", local_time(action["time_ns"]))
        if action["action"] == "ADMITTED":
            entry.setdefault("started_at", local_time(action["time_ns"]))
        if action["action"] == "TERMINATED":
            entry["finished_at"] = local_time(action["time_ns"])
    owners = set(state["owners"].values())
    runs = []
    for index, item in enumerate(state["runs"]):
        runs.append({**item, **times.get(item["run_id"], {}), "enqueue_order": index,
                     "is_owner": item["run_id"] in owners,
                     "has_executed": item["started"],
                     "priority": state["tasks"][item["task_id"]]["priority"]})
    current = [r for r in runs if r["is_owner"]]
    queue = sorted((r for r in runs if r["state"] in {"queued", "suspended"}),
                   key=lambda r: (-r["priority"], r["enqueue_order"]))
    executed = [r for r in runs if r["has_executed"]]
    counts = Counter(r["task_id"] for r in executed)
    latest = {r["task_id"]: r for r in runs}
    tasks = []
    for name, definition in configuration["tasks"].items():
        last = latest.get(name)
        tasks.append({**definition, **state["tasks"][name], "task_id": name,
                      "executed_count": counts[name],
                      "last_state": None if last is None else last["state"],
                      "next_evaluation_at": local_time(state["tasks"][name]["next_evaluation_ns"])})
    busy = {r["task_id"] for r in current + queue}
    waiting = [t for t in tasks if t["enabled"] and not t["builtin"] and t["task_id"] not in busy]
    execution = state.get("execution", {})
    outcomes = Counter(r["state"] for r in executed if r["state"] in TERMINAL_STATES)
    return {
        "enabled": True, "lifecycle": state["lifecycle"], "paused": state["paused"],
        "revision": revision, "snapshot_time": local_time(state.get("time_ns")),
        "timezone": configuration["timezone"], "configuration": configuration,
        "summary": {"configured": len(tasks), "enabled": sum(t["enabled"] for t in tasks),
                    "current": len(current), "queued": len(queue), "executed": len(executed),
                    "succeeded": outcomes["succeeded"],
                    "failed": outcomes["failed"] + outcomes["unachievable"],
                    "finished": sum(outcomes.values())},
        "tasks": tasks, "current": current, "queue": queue, "waiting": waiting,
        # Ownership epochs reflect actual admission/resumption order. Queue
        # insertion order can differ when a higher-priority task executes first.
        "history": sorted(executed, key=lambda r: r["epoch"], reverse=True)[:history_limit],
        "history_total": len(executed),
        "actions": [{**a, "time_ns": str(a["time_ns"]), "time": local_time(a["time_ns"])}
                    for a in reversed(actions[-100:])],
        "execution": {"version": execution.get("version"), "complete": execution.get("complete"),
                      "pending_orders": execution.get("pending_orders"),
                      "remaining_instruments": sum(Decimal(str(v)) != 0
                                                   for v in execution.get("remaining", {}).values()),
                      "frozen_instruments": len(execution.get("frozen", {}))},
    }
