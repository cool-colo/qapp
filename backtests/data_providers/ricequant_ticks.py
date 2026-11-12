from __future__ import annotations

from datetime import timedelta
import math

import pandas as pd

from backtests.data_providers.clickhouse import ClickHouseBarDataProvider
from backtests.data_providers.clickhouse import ClickHouseBarSchema
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.clickhouse import quote_identifier
from backtests.data_providers.clickhouse import quote_literal
from backtests.data_providers.historical_ticks import HistoricalTick
from backtests.data_providers.historical_ticks import continuous_session_index
from backtests.data_providers.historical_ticks import validate_final_auction_results


def ricequant_symbol(stock_code: str) -> str:
    symbol, exchange = stock_code.rsplit(".", 1)
    exchanges = {"SZ": "XSHE", "SH": "XSHG"}
    if exchange not in exchanges:
        raise ValueError(f"Unsupported stock code: {stock_code}")
    return f"{symbol}.{exchanges[exchange]}"


class ClickHouseRicequantTickDataProvider:
    """Ordinary RiceQuant five-level ticks; no historical L2 feed is needed.

    ``datetime`` is exchange time. Database ``_ingest_time`` is NOT historical
    receive time, so an explicit simulated data delay supplies ``ts_init``.
    The 09:25 cumulative volume is the opening auction's final成交量.
    """

    def __init__(self, connection: ClickHouseConnectionConfig,
                 table: str = "ricequant_stock_tick", data_latency_ms: float = 0):
        if not math.isfinite(data_latency_ms) or data_latency_ms < 0:
            raise ValueError("data_latency_ms must be finite and non-negative")
        self.table = quote_identifier(table)
        self.delay_ns = int(data_latency_ms * 1_000_000)
        self.client = ClickHouseBarDataProvider(connection, ClickHouseBarSchema())

    def load(self, *, start: str, end: str, symbols: set[str]) -> list[HistoricalTick]:
        if not symbols:
            raise ValueError("RiceQuant tick replay requires a nonempty universe")
        # The inspected RiceQuant source covers SH/SZ only. Do not invent a BJ
        # symbol suffix or silently omit selected instruments.
        mapping = {ricequant_symbol(code): code for code in symbols}
        # Restrict to selected strategy candidates and daily partitions. Neither
        # the final-per-day DWD table nor an unbounded whole-market scan is used.
        result = []
        day = pd.Timestamp(start).date()
        last_day = pd.Timestamp(end).date()
        while day <= last_day:
            for offset in range(0, len(mapping), 100):
                chunk = sorted(mapping)[offset:offset + 100]
                sql = f"""
SELECT order_book_id, datetime, tick_sequence, last, open, prev_close, volume,
       total_turnover, a1, a1_v, b1, b1_v
FROM {self.table}
PREWHERE trading_date = toDate({quote_literal(day.isoformat())})
     AND order_book_id IN ({','.join(quote_literal(code) for code in chunk)})
WHERE datetime >= toDateTime64({quote_literal(day.isoformat() + ' 09:15:00')},9,'Asia/Shanghai')
  AND datetime <= toDateTime64({quote_literal(day.isoformat() + ' 15:00:00')},9,'Asia/Shanghai')
ORDER BY datetime, order_book_id, tick_sequence
FORMAT JSONEachRow
"""
                result.extend(self.build_ticks(self.client.fetch_json_each_row(sql), mapping))
            day += timedelta(days=1)
        if not result:
            raise ValueError("No usable ricequant_stock_tick data in requested dates/universe")
        validate_final_auction_results(result)
        result.sort(key=lambda tick: (tick.ts_init, tick.symbol, tick.ts_event))
        return result

    def build_ticks(self, rows: list[dict], mapping: dict[str, str]) -> list[HistoricalTick]:
        result = []
        previous = {}
        for raw in rows:
            symbol = mapping[raw["order_book_id"]]
            event = pd.Timestamp(raw["datetime"], tz="Asia/Shanghai")
            key = (symbol, event.value)
            if key in previous:
                raise ValueError(f"Ambiguous RiceQuant tick timestamp: {symbol} {event}")
            previous[key] = raw
            clock_time = event.strftime("%H:%M:%S")
            close = raw["prev_close"]
            if close is None or not math.isfinite(close) or close <= 0:
                continue
            if "09:15:00" <= clock_time < "09:25:00":
                if raw["a1"] is None or raw["b1"] != raw["a1"] or raw["a1"] <= 0:
                    continue
                if raw["open"] not in (None, 0):
                    raise ValueError(f"RiceQuant indicative tick contains future open: {symbol} {event}")
                price = float(raw["a1"])
                if not math.isfinite(price):
                    continue
                result.append(HistoricalTick(symbol, "indicative", event.value,
                    event.value + self.delay_ns, price, price, 1, 1, float(close), 0))
            elif clock_time == "09:25:00":
                price, volume = raw["open"], raw["volume"]
                if (price is None or volume is None or price <= 0 or volume <= 0
                        or not math.isfinite(price) or not math.isfinite(volume)):
                    continue
                if int(volume) != volume:
                    raise ValueError("RiceQuant auction volume must be whole shares")
                result.append(HistoricalTick(symbol, "auction", event.value,
                    event.value + self.delay_ns, float(price), float(price), int(volume),
                    int(volume), float(close), float(price)))
            elif (continuous_session_index(event.time()) is not None
                  or "14:57:00" <= clock_time <= "15:00:00"):
                values = (raw["a1"], raw["b1"], raw["a1_v"], raw["b1_v"], raw["open"])
                if any(value is None or not math.isfinite(value) or value <= 0 for value in values):
                    continue  # No executable two-sided quote, e.g. suspension/limit lock.
                if raw["b1"] > raw["a1"]:
                    raise ValueError(f"Crossed continuous RiceQuant quote: {symbol} {event}")
                if int(raw["a1_v"]) != raw["a1_v"] or int(raw["b1_v"]) != raw["b1_v"]:
                    raise ValueError("RiceQuant quote quantities must be whole shares")
                # A day's cumulative volume is not verified final closing-auction
                # volume. Preserve closing observations as information only.
                kind = "quote" if continuous_session_index(event.time()) is not None else "closing_indicative"
                result.append(HistoricalTick(symbol, kind, event.value,
                    event.value + self.delay_ns, float(raw["b1"]), float(raw["a1"]),
                    int(raw["b1_v"]), int(raw["a1_v"]), float(close), float(raw["open"])))
        validate_final_auction_results(result)
        return result
