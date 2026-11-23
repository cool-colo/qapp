from __future__ import annotations
from datetime import date
from typing import Any, Protocol
import math
import pandas as pd
from strategies.tick_snapshot import TickSnapshot
from strategies.opening_auction import auction_time
from strategies.model_target_planners.base import ModelTargetPlanningContext, ModelTargetInstrumentContext


class PlanningContextProvider(Protocol):
    def build(self, trading_date: date, *, stage: str | None = None,
              snapshot: dict[str, dict[str, Any]] | None = None,
              ts_init: int | None = None) -> ModelTargetPlanningContext: ...


class PlanningContextBuilder:
    def __init__(self, host):
        self.host = host

    def build(
        self, trading_date: date, *, stage: str | None = None,
        snapshot: dict[str, dict[str, Any]] | None = None,
        ts_init: int | None = None,
    ) -> ModelTargetPlanningContext:
        """Capture inputs once; planning helpers never reread mutable price caches."""
        if stage is None:
            now = pd.Timestamp(self.host.clock.utc_now()).tz_convert(self.host.config.timezone_name)
            stage = "pre_open" if now.time() < auction_time("09:30") else "continuous"
        instruments: dict[str, ModelTargetInstrumentContext] = {}
        if snapshot is None:
            # Preserve the existing bar-based path when no full-tick was supplied.
            for i in self.host._today_open.keys() | self.host._last_close.keys() | self.host._market_status.keys():
                tick = self.host._market_status.get(i)
                if tick is None:
                    price = self.host._today_open.get(i)
                    source = "open" if price is not None else None
                else:
                    price = tick.last_price
                    if price is not None and (not math.isfinite(price) or price <= 0):
                        price = None
                    source = "full_tick_last" if price is not None else None
                instruments[i] = ModelTargetInstrumentContext(
                    price=price, open_price=self.host._today_open.get(i),
                    previous_close=self.host._last_close.get(i),
                    market_status=None if tick is None else tick.market_status,
                    price_source=source,
                    price_ts_event=None if tick is None else tick.ts_event,
                )
        else:
            instruments = {i: ModelTargetInstrumentContext(previous_close=close)
                           for i, close in self.host._last_close.items()}
            for instrument_id, fields in snapshot.items():
                i = str(instrument_id)
                tick = TickSnapshot.from_fields(fields)
                price = tick.last_price
                if price is not None and (not math.isfinite(price) or price <= 0):
                    price = None
                open_price = tick.open
                if open_price is not None and (not math.isfinite(open_price) or open_price <= 0):
                    open_price = None
                close = tick.last_close
                if close is None or not math.isfinite(close) or close <= 0:
                    close = self.host._last_close.get(i)
                instruments[i] = ModelTargetInstrumentContext(
                    price=price, open_price=open_price, previous_close=close,
                    market_status=tick.market_status,
                    price_source=("auction_indicative" if stage == "opening_auction" else "full_tick_last")
                                 if price is not None else None,
                    price_ts_event=tick.ts_event,
                )
        return ModelTargetPlanningContext(
            trading_date=trading_date, stage=stage, instruments=instruments,
            ts_init=(self.host._market_status_updated_at_ns if snapshot is None and ts_init is None else ts_init),
        )

