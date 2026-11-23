from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from enum import Enum
from collections.abc import Mapping
from typing import Any

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
    inputs: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    validity: ReplanValidity = dataclasses.field(default_factory=ExecutionWindowReplanValidity)

    def payload(self, plan: ModelTargetPlan | None = None) -> dict[str, Any]:
        return {**planning_json(self), "plan": planning_json(plan)}
