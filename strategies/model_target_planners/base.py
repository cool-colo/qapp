from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class RequestInfo:
    price: float | None = None
    price_source: str | None = None
    score: float | None = None
    expected_return: float | None = None
    current_qty: int | None = None
    recent_target_date: date | None = None
    recent_holding_days: int | None = None


@dataclass(frozen=True)
class TargetInfo:
    stock_code: str
    weight: float | None
    quantity: int | None
    request_info: RequestInfo
    target_version: str | None
    instrument_id: str
    is_locked: bool | None


@dataclass(frozen=True)
class ModelTargetPlan:
    trading_date: date
    signal_date: date | None
    targets: list[TargetInfo]
    reason: str
    request_id: str | None = None
    total_asset: float | None = None  # raw total asset
    investable_asset: float | None = None  # total asset net of trading buffer


@dataclass(frozen=True)
class ModelTargetCandidate:
    instrument_id: str
    stock_code: str
    score: float
    open_price: float | None = None  # pre-market open (falls back to prev close upstream)
    expected_return: float | None = None  # daily_model_predictions.pred_return_live


@dataclass(frozen=True)
class CurrentHolding:
    """
    A currently-held position sent to the risk manager as ``current_weights``.

    ``recent_target_date`` is the internal name for the most recent date this stock
    carried a positive target (last ``target_qty > 0`` in live, or the entry date in
    backtests). It is serialized on the wire as ``recent_buy_date``.
    """

    instrument_id: str
    stock_code: str
    quantity: int
    price: float  # current price, defaults to today's open
    recent_target_date: date | None = None
    recent_holding_days: int = 0


@dataclass(frozen=False)
class ModelTargetPlanningRequest:
    trading_date: date
    signal_date: date | None
    active_instrument_ids: list[str]
    candidates: list[ModelTargetCandidate]
    current_holdings: list[CurrentHolding]
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
