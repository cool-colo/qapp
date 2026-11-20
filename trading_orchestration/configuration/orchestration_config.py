from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import math

from task_orchestration.models import TaskSpec
from trading_orchestration.configuration.exposure_config import ExposureConfig


@dataclass(frozen=True)
class OrchestrationConfig:
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    poll_interval_secs: float = 0.5
    tasks: Mapping[str, TaskSpec] = field(default_factory=dict)
    exposure_components: Mapping[str, str] = field(default_factory=dict)
    execution_components: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> OrchestrationConfig:
        import yaml
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict) or set(raw) - {"schema_version", "poll_interval_secs", "exposure", "tasks", "exposure_components"}:
            raise ValueError("invalid orchestration configuration fields")
        if raw.get("schema_version") != 1:
            raise ValueError("orchestration schema_version must be 1")
        exposure_raw = raw.get("exposure", {})
        if not isinstance(exposure_raw, dict) or set(exposure_raw) - {f.name for f in fields(ExposureConfig)}:
            raise ValueError("invalid exposure configuration fields")
        exposure_raw = dict(exposure_raw)
        for name in ("tolerance", "commission_rate", "minimum_fee"):
            if name in exposure_raw:
                exposure_raw[name] = Decimal(str(exposure_raw[name]))
        exposure = ExposureConfig(**exposure_raw)
        from strategies.cancellation import parse_cancel_windows
        windows = parse_cancel_windows(exposure.windows)
        from datetime import time
        if not windows or any(not any(a <= start < end <= b for a, b in (
                (time(9, 30), time(11, 30)), (time(13), time(14, 57)))) for start, end in windows):
            raise ValueError("exposure windows must be inside continuous sessions before 14:57")
        interval = float(raw.get("poll_interval_secs", 0.5))
        if not math.isfinite(interval) or interval <= 0 or interval > exposure.interval_secs:
            raise ValueError("invalid polling interval")
        defaults = {"daily_planning": 100, "normal_execution": 100, "auction_planning": 100,
                    "auction_execution": 100, "scheduled_replan": 200, "price_limit_replan": 200,
                    "manual_replan": 500, "manual_execution": 500,
                    "exposure_correction": exposure.priority}
        configured = raw.get("tasks", {})
        if not isinstance(configured, dict) or set(configured) - defaults.keys():
            raise ValueError("unknown task definition")
        result = {}
        profiles = {}
        for name, priority in defaults.items():
            values = configured.get(name, {})
            if not isinstance(values, dict) or set(values) - {"enabled", "priority", "can_preempt", "preemptible", "splitter", "cancellation"}:
                raise ValueError(f"invalid task configuration: {name}")
            values = dict(values)
            profile = {"splitter": values.pop("splitter", "existing_notional"),
                       "cancellation": values.pop("cancellation", "existing")}
            if any(not isinstance(value, str) or not value for value in profile.values()):
                raise ValueError("execution component IDs must be nonempty strings")
            profiles[name] = MappingProxyType(profile)
            for key in ("enabled", "can_preempt", "preemptible"):
                if key in values and not isinstance(values[key], bool):
                    raise ValueError(f"{name}.{key} must be boolean")
            chosen_priority = values.pop("priority", priority)
            if not isinstance(chosen_priority, int) or isinstance(chosen_priority, bool):
                raise ValueError("priority must be integer")
            enabled = values.pop("enabled", exposure.enabled if name == "exposure_correction" else True)
            result[name] = TaskSpec(name, chosen_priority, enabled=enabled, **values,
                                   interval_ns=int(exposure.interval_secs * 1e9)
                                   if name == "exposure_correction" else int(interval * 1e9))
        components = {"assessor": "position_ratio", "planner": "remaining_target",
                      "executor": "nautilus_exposure", "stabilizer": "cancel_reconcile",
                      "risk_planner": "risk_manager", "validator": "formal_remainder",
                      "outcome": "capacity_replan"}
        custom = raw.get("exposure_components", {})
        if not isinstance(custom, dict) or set(custom) - components.keys():
            raise ValueError("unknown exposure component role")
        if any(not isinstance(value, str) or not value for value in custom.values()):
            raise ValueError("component IDs must be nonempty strings")
        components.update(custom)
        return cls(exposure, interval, MappingProxyType(result), MappingProxyType(components),
                   MappingProxyType(profiles))

