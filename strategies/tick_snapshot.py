from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from nautilus_trader.model.enums import MarketStatusAction

_OPEN_INT_TO_MARKET_STATUS: dict[int, MarketStatusAction] = {
    1: MarketStatusAction.SUSPEND,  # 停牌
    11: MarketStatusAction.PRE_OPEN,  # 开盘前 (S)
    12: MarketStatusAction.PRE_OPEN,  # 集合竞价时段 (C)
    13: MarketStatusAction.TRADING,  # 连续交易时段 (T)
    14: MarketStatusAction.PAUSE,  # 休市 (B)
    15: MarketStatusAction.CLOSE,  # 闭市 (E)
    16: MarketStatusAction.PAUSE,  # 波动性中断 (V)
    17: MarketStatusAction.PAUSE,  # 临时停牌 (P)
    18: MarketStatusAction.PRE_CLOSE,  # 收盘集合竞价 (U)
    19: MarketStatusAction.PRE_OPEN,  # 盘中集合竞价 (M)
    20: MarketStatusAction.HALT,  # 暂停交易至闭市 (N)
    22: MarketStatusAction.POST_CLOSE,  # 盘后固定价格行情
    23: MarketStatusAction.CLOSE,  # 盘后固定价格行情完毕
}

@dataclass(frozen=True)
class TickSnapshot:
    """Latest normalized QMT full-tick values for one instrument."""

    market_status: MarketStatusAction | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    last_close: float | None = None
    amount: float | None = None
    volume: int | None = None
    pvolume: int | None = None
    open_int: int | None = None
    last_settlement_price: float | None = None
    ts_event: int | None = None

    @classmethod
    def from_fields(cls, fields: Any) -> "TickSnapshot":
        if not isinstance(fields, Mapping):
            return cls()
        open_int = cls._int_or_none(fields.get("open_int", fields.get("openInt")))
        market_status = (
            None
            if open_int is None
            else _OPEN_INT_TO_MARKET_STATUS.get(open_int, MarketStatusAction.NONE)
        )
        return cls(
            market_status=market_status,
            last_price=cls._float_or_none(fields.get("last_price")),
            open=cls._float_or_none(fields.get("open")),
            high=cls._float_or_none(fields.get("high")),
            low=cls._float_or_none(fields.get("low")),
            last_close=cls._float_or_none(fields.get("last_close")),
            amount=cls._float_or_none(fields.get("amount")),
            volume=cls._int_or_none(fields.get("volume")),
            pvolume=cls._int_or_none(fields.get("pvolume")),
            open_int=open_int,
            last_settlement_price=cls._float_or_none(fields.get("last_settlement_price")),
            ts_event=cls._int_or_none(fields.get("ts_event")),
        )

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


