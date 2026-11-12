from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time
from typing import Any

import pandas as pd


def auction_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid auction time: {value!r}") from exc


def validate_auction_config(config: Any) -> None:
    prepare = auction_time(config.auction_prepare_time)
    submit = auction_time(config.auction_submit_time)
    cutoff = auction_time(config.auction_submit_cutoff)
    if not time(9, 20) <= prepare < submit < cutoff < time(9, 25):
        raise ValueError("Auction times must satisfy 09:20 <= prepare < submit < cutoff < 09:25")
    for name, value in (
        ("auction_reference_max_age_secs", config.auction_reference_max_age_secs),
        ("auction_buy_offset_bps", config.auction_buy_offset_bps),
        ("auction_sell_offset_bps", config.auction_sell_offset_bps),
        ("auction_commission_rate", config.auction_commission_rate),
        ("auction_minimum_fee", config.auction_minimum_fee),
        ("auction_partial_min_notional", config.auction_partial_min_notional),
    ):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if config.auction_reference_max_age_secs <= 0:
        raise ValueError("auction_reference_max_age_secs must be positive")
    if config.auction_commission_rate >= 1:
        raise ValueError("auction_commission_rate must be less than 1")


@dataclass(frozen=True)
class AuctionReference:
    price: float
    ts_event: int
    ts_init: int
    source: str = "auction_indicative"

    def is_fresh(self, now_ns: int, timezone: str, max_age_secs: float) -> bool:
        if not math.isfinite(self.price) or self.price <= 0:
            return False
        event = pd.Timestamp(self.ts_event, unit="ns", tz="UTC").tz_convert(timezone)
        now = pd.Timestamp(now_ns, unit="ns", tz="UTC").tz_convert(timezone)
        return (
            event.date() == now.date()
            and time(9, 15) <= event.time() < time(9, 25)
            and self.ts_event <= self.ts_init <= now_ns
            and 0 <= now_ns - self.ts_event <= max_age_secs * 1_000_000_000
        )


