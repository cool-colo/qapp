from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time
from typing import Any


def auction_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid auction time: {value!r}") from exc


def validate_auction_config(config: Any) -> None:
    start = auction_time(config.auction_start_time)
    execution = auction_time(config.auction_execution_time)
    cutoff = auction_time(config.auction_submit_cutoff)
    if not time(9, 15) <= start < execution < cutoff <= time(15, 0):
        raise ValueError("Auction times must satisfy 09:15 <= start < execution < cutoff <= 15:00")
    for name, value in (
        ("auction_buy_offset_bps", config.auction_buy_offset_bps),
        ("auction_sell_offset_bps", config.auction_sell_offset_bps),
        ("auction_commission_rate", config.auction_commission_rate),
        ("auction_minimum_fee", config.auction_minimum_fee),
        ("auction_min_buy_notional", config.auction_min_buy_notional),
    ):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if config.auction_commission_rate >= 1:
        raise ValueError("auction_commission_rate must be less than 1")


@dataclass(frozen=True)
class AuctionReference:
    price: float
    ts_event: int | None
    ts_init: int
    source: str = "auction_indicative"
