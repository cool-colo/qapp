from __future__ import annotations

from strategies.model_target_planners.base import ModelTargetPlan
from strategies.model_target_planners.base import ModelTargetPlanner
from strategies.model_target_planners.base import ModelTargetPlanningRequest


class EqualWeightModelTargetPlanner(ModelTargetPlanner):
    reason = "model_prediction_score"

    def plan(self, request: ModelTargetPlanningRequest) -> ModelTargetPlan:
        active_ids = sorted(request.active_instrument_ids)
        if not active_ids:
            return ModelTargetPlan(request.trading_date, request.signal_date, {}, self.reason)
        gross_weight = max(0.0, min(1.0, 1.0 - float(request.target_cash_buffer_percent)))
        target_weight = min(float(request.max_position_percent), gross_weight / len(active_ids))
        return ModelTargetPlan(
            trading_date=request.trading_date,
            signal_date=request.signal_date,
            weights={instrument_id: target_weight for instrument_id in active_ids},
            reason=self.reason,
        )
