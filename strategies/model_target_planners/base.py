from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ModelTargetPlan:
    trading_date: date
    signal_date: date | None
    weights: dict[str, float]
    reason: str


@dataclass(frozen=True)
class ModelTargetCandidate:
    instrument_id: str
    stock_code: str
    score: float


@dataclass(frozen=False)
class ModelTargetPlanningRequest:
    trading_date: date
    signal_date: date | None
    active_instrument_ids: list[str]
    candidates: list[ModelTargetCandidate]
    current_weights: dict[str, float]
    target_cash_buffer_percent: float
    max_position_percent: float


class ModelTargetPlanner:
    def plan(self, request: ModelTargetPlanningRequest) -> ModelTargetPlan:
        raise NotImplementedError


def normalize_stock_code(value: Any) -> str:
    return str(value or "").strip().upper()
