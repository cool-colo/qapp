from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class HistoricalTick:
    symbol: str
    kind: str
    ts_event: int
    ts_init: int
    bid_price: float
    ask_price: float
    bid_size: int
    ask_size: int
    previous_close: float
    open_price: float


def continuous_session_index(clock_time: time) -> int | None:
    """Exchange continuous sessions; closing indicative quotes cannot execute."""
    if time(9, 30) <= clock_time <= time(11, 30):
        return 0
    if time(13) <= clock_time < time(14, 57):
        return 1
    return None


def validate_final_auction_results(rows: Iterable[HistoricalTick],
                                   timezone: str = "Asia/Shanghai") -> None:
    seen = set()
    for row in rows:
        if row.kind not in {"auction", "closing_auction"}:
            continue
        day = pd.Timestamp(row.ts_event, unit="ns", tz="UTC").tz_convert(timezone).date()
        key = (row.symbol, day, row.kind)
        if key in seen:
            raise ValueError(f"Multiple final {row.kind} results for {row.symbol} on {day}")
        seen.add(key)


class HistoricalTickDataProvider:
    """Import explicit auction results and share-denominated quotes from CSV.

    Timestamps must include a timezone. ``auction`` rows contain the final
    uniform price on both sides and final auction volume on both sizes. An
    indicative quote is information only, never executable liquidity.
    """

    COLUMNS = {
        "symbol", "kind", "ts_event", "ts_init", "bid_price", "ask_price",
        "bid_size", "ask_size", "previous_close", "open_price",
    }

    def load(self, path: str, *, start: str, end: str,
             symbols: set[str], timezone: str = "Asia/Shanghai") -> list[HistoricalTick]:
        result = []
        keys = set()
        with Path(path).open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if set(reader.fieldnames or ()) != self.COLUMNS:
                raise ValueError(f"Historical tick CSV must have columns: {sorted(self.COLUMNS)}")
            for number, raw in enumerate(reader, 2):
                if raw["symbol"] not in symbols:
                    continue
                try:
                    event = pd.Timestamp(raw["ts_event"])
                    init = pd.Timestamp(raw["ts_init"])
                    if event.tzinfo is None or init.tzinfo is None:
                        raise ValueError("timestamps must include timezone")
                    local = event.tz_convert(timezone)
                    if not pd.Timestamp(start).date() <= local.date() <= pd.Timestamp(end).date():
                        continue
                    tick = HistoricalTick(
                        symbol=raw["symbol"], kind=raw["kind"],
                        ts_event=event.value, ts_init=init.value,
                        bid_price=float(raw["bid_price"]), ask_price=float(raw["ask_price"]),
                        bid_size=int(raw["bid_size"]), ask_size=int(raw["ask_size"]),
                        previous_close=float(raw["previous_close"]),
                        open_price=float(raw["open_price"]),
                    )
                    prices = (tick.bid_price, tick.ask_price, tick.previous_close)
                    if (not all(math.isfinite(p) and p > 0 for p in prices)
                            or not math.isfinite(tick.open_price) or tick.open_price < 0
                            or tick.bid_price > tick.ask_price
                            or min(tick.bid_size, tick.ask_size) <= 0
                            or tick.ts_event > tick.ts_init
                            or init.tz_convert(timezone).date() != local.date()):
                        raise ValueError("invalid quote prices, sizes or timestamps")
                    clock_time = local.strftime("%H:%M:%S")
                    if tick.kind == "indicative":
                        if not "09:15:00" <= clock_time < "09:25:00" or tick.open_price != 0:
                            raise ValueError("indicative rows must precede 09:25 and have open_price=0")
                        if tick.bid_price != tick.ask_price:
                            raise ValueError("indicative price must be identical on both sides")
                    elif tick.kind == "auction":
                        if (clock_time != "09:25:00" or tick.bid_price != tick.ask_price
                                or tick.bid_size != tick.ask_size or tick.open_price != tick.bid_price):
                            raise ValueError("auction rows require 09:25 uniform price and final volume")
                    elif tick.kind == "quote":
                        if continuous_session_index(local.time()) is None or tick.open_price <= 0:
                            raise ValueError("quote rows require a continuous session and actual open")
                    elif tick.kind == "closing_indicative":
                        if not "14:57:00" <= clock_time <= "15:00:00" or tick.open_price <= 0:
                            raise ValueError("closing_indicative rows require 14:57-15:00 and actual open")
                    elif tick.kind == "closing_auction":
                        if (clock_time != "15:00:00" or tick.bid_price != tick.ask_price
                                or tick.bid_size != tick.ask_size or tick.open_price <= 0):
                            raise ValueError("closing_auction rows require 15:00 uniform price and final volume")
                    else:
                        raise ValueError("kind must be indicative, auction, quote, closing_indicative or closing_auction")
                    key = (tick.symbol, tick.ts_event)
                    if key in keys:
                        raise ValueError("duplicate symbol/event timestamp")
                    keys.add(key)
                    result.append(tick)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Historical tick CSV row {number}: {exc}") from exc
        if not result:
            raise ValueError("No historical ticks in requested dates/universe")
        validate_final_auction_results(result, timezone)
        result.sort(key=lambda tick: (tick.ts_init, tick.symbol, tick.ts_event))
        return result
