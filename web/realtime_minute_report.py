"""Read the latest whole observation of each RiceQuant realtime minute bar."""

from __future__ import annotations

from datetime import date, datetime, time
from math import isfinite
from typing import Any

from web.db import ClickHouseClient, quote_literal, ricequant_order_book_id


def realtime_minute_query(stock_code: str, trading_date: str) -> str:
    day = date.fromisoformat(trading_date).isoformat()
    symbol = ricequant_order_book_id(stock_code)
    return f"""
    SELECT toString(datetime) AS ts,
           tupleElement(latest, 1) AS open, tupleElement(latest, 2) AS high,
           tupleElement(latest, 3) AS low, tupleElement(latest, 4) AS close,
           tupleElement(latest, 5) AS volume, tupleElement(latest, 6) AS total_turnover
    FROM (
        SELECT datetime,
               argMax(tuple(open, high, low, close, volume, total_turnover),
                      tuple(received_at, event_id)) AS latest
        FROM ricequant_stock_minute_realtime
        WHERE trade_date = toDate({quote_literal(day)})
          AND order_book_id = {quote_literal(symbol)}
          AND (
              datetime BETWEEN toDateTime64('{day} 09:30:00', 3, 'Asia/Shanghai')
                           AND toDateTime64('{day} 11:30:00', 3, 'Asia/Shanghai')
              OR datetime BETWEEN toDateTime64('{day} 13:00:00', 3, 'Asia/Shanghai')
                              AND toDateTime64('{day} 15:00:00', 3, 'Asia/Shanghai')
          )
          AND datetime <= now64(3, 'Asia/Shanghai')
          AND received_at <= now64(6, 'Asia/Shanghai')
        GROUP BY datetime
    )
    ORDER BY datetime
    """


def query_realtime_minutes(
    clickhouse: ClickHouseClient, stock_code: str, trading_date: str,
) -> dict[str, Any]:
    day = date.fromisoformat(trading_date).isoformat()
    rows = clickhouse.query(realtime_minute_query(stock_code, day))
    bars = []
    for row in rows:
        bar = {"ts": str(row["ts"])}
        for field in ("open", "high", "low", "close", "volume", "total_turnover"):
            value = row[field]
            number = float(value) if value is not None else None
            valid = number is not None and isfinite(number)
            if valid:
                valid = number >= 0 if field in ("volume", "total_turnover") else number > 0
            bar[field] = number if valid else None
        bars.append(bar)
    bars.sort(key=lambda bar: bar["ts"])
    opening = None
    if bars and datetime.fromisoformat(bars[0]["ts"]).time() <= time(9, 31):
        opening = bars[0]["open"]
    for bar in bars:
        bar["price_change_bps"] = (
            (bar["close"] / opening - 1) * 10_000
            if opening is not None and bar["close"] is not None else None
        )
    return {
        "stock_code": stock_code.strip().upper(),
        "trade_date": day,
        "open_price": opening,
        "bars": bars,
    }
