from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ModelTargetPlan:
    trading_date: date
    signal_date: date | None
    weights: dict[str, float]
    reason: str
    request_id: str | None = None
    # instrument_id -> committed target share count (固定目标股数). Empty when the
    # planner sizes by weight only (e.g. the equal-weight fallback). ``0`` is a valid
    # entry (liquidate / hold-none) and is retained, not dropped.
    target_qty: dict[str, int] = field(default_factory=dict)
    # Audit fields describing the sizing inputs the plan was built from. Filled by the
    # strategy layer (planners stay unaware of price provenance / asset accounting) so
    # both the bar path and the snapshot recorder persist consistent values.
    open_prices: dict[str, float] = field(default_factory=dict)
    price_sources: dict[str, str] = field(default_factory=dict)  # instrument_id -> open|prev_close
    total_asset: float | None = None  # raw total asset
    investable_asset: float | None = None  # total asset net of trading buffer


@dataclass(frozen=True)
class ModelTargetCandidate:
    instrument_id: str
    stock_code: str
    score: float
    open_price: float | None = None  # pre-market open (falls back to prev close upstream)


@dataclass(frozen=False)
class ModelTargetPlanningRequest:
    trading_date: date
    signal_date: date | None
    active_instrument_ids: list[str]
    candidates: list[ModelTargetCandidate]
    current_weights: dict[str, float]
    target_cash_buffer_percent: float
    max_position_percent: float
    total_asset: float | None = None  # raw total asset
    investable_asset: float | None = None  # total asset net of trading buffer (sizing basis)
    open_prices: dict[str, float] = field(default_factory=dict)  # instrument_id -> price


class ModelTargetPlanner:
    def plan(self, request: ModelTargetPlanningRequest) -> ModelTargetPlan:
        raise NotImplementedError


def normalize_stock_code(value: Any) -> str:
    return str(value or "").strip().upper()
