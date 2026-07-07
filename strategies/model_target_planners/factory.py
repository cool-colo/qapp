from __future__ import annotations

from typing import Any

from strategies.model_target_planners.base import ModelTargetPlanner
from strategies.model_target_planners.equal_weight import EqualWeightModelTargetPlanner
from strategies.model_target_planners.risk_manager import RiskManagerModelTargetPlanner


def build_model_target_planner(config: Any) -> ModelTargetPlanner:
    planner = str(config.target_weight_planner or "equal_weight").strip().lower()
    if planner in {"equal", "equal_weight", "fixed", "fixed_weight"}:
        return EqualWeightModelTargetPlanner()
    if planner in {"risk_manager", "risk_manager_optimize", "optimizer"}:
        return RiskManagerModelTargetPlanner(
            base_url=config.risk_manager_base_url,
            risk_model_id=config.risk_manager_risk_model_id,
            mode=config.risk_manager_mode,
            timeout_secs=config.risk_manager_timeout_secs,
        )
    raise ValueError(f"unknown target_weight_planner: {config.target_weight_planner}")
