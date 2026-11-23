from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from trading_orchestration.configuration.orchestration_config import OrchestrationConfig


class StrategySchedule(Protocol):
    timezone_name: str
    auction_enabled: bool
    auction_start_time: str
    auction_execution_time: str
    auction_submit_cutoff: str
    execution_windows: str
    scheduled_replan_times: tuple[str, ...]


def describe_scheduler(config: OrchestrationConfig, strategy: StrategySchedule) -> dict:
    """Describe immutable configuration once, without evaluating any trading rule."""
    triggers = {
        "bootstrap": {"kind": "startup"},
        "daily_planning": {"kind": "event"},
        "auction_planning": {"kind": "time", "times": [strategy.auction_start_time],
                             "applicable": strategy.auction_enabled},
        "auction_execution": {"kind": "time", "times": [strategy.auction_execution_time],
                              "cutoff": strategy.auction_submit_cutoff,
                              "applicable": strategy.auction_enabled},
        "normal_execution": {"kind": "condition", "windows": strategy.execution_windows},
        "scheduled_replan": {"kind": "time", "times": list(strategy.scheduled_replan_times)},
        "price_limit_replan": {"kind": "condition"},
        "exposure_correction": {"kind": "condition", "windows": config.exposure.windows},
        "manual_replan": {"kind": "manual"},
        "manual_execution": {"kind": "manual"},
    }
    definitions = {
        "bootstrap": {"priority": 1000, "can_preempt": True, "preemptible": False,
                      "interval_ns": 1_000_000_000, "scope": "default",
                      "trigger": triggers["bootstrap"], "builtin": True},
    }
    for name, spec in config.tasks.items():
        definitions[name] = {
            **asdict(spec), **dict(config.execution_components[name]),
            **({"planner": config.planning_components[name]} if name in config.planning_components else {}),
            "trigger": triggers[name], "builtin": False,
        }
    exposure = asdict(config.exposure)
    for key in ("tolerance", "commission_rate", "minimum_fee"):
        exposure[key] = str(exposure[key])
    return {"timezone": strategy.timezone_name, "poll_interval_secs": config.poll_interval_secs,
            "tasks": definitions, "exposure": exposure,
            "cleanup_component": config.cleanup_component,
            "cleanup_reconciliation_enabled": config.cleanup_reconciliation_enabled,
            "exposure_components": dict(config.exposure_components)}
