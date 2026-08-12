from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


@dataclass(frozen=True)
class DailyStockData:
    """Reusable end-of-day market and price-limit data for one stock/date."""

    stock_code: str
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    pre_close: float | None = None
    up_limit: float | None = None
    down_limit: float | None = None


def index_daily_stock_data(
    rows: Iterable[DailyStockData],
) -> dict[tuple[str, date], DailyStockData]:
    """Index rows by normalized stock/date and fail on ambiguous duplicates."""

    result: dict[tuple[str, date], DailyStockData] = {}
    for row in rows:
        key = (row.stock_code, row.trade_date)
        if key in result:
            raise ValueError(
                f"Duplicate daily stock data for stock_code={row.stock_code} "
                f"trade_date={row.trade_date}",
            )
        result[key] = row
    return result


def latest_closes(
    rows: Iterable[DailyStockData],
    as_of_date: date,
) -> dict[str, float]:
    """Return each stock's latest positive close on or before ``as_of_date``."""

    latest: dict[str, tuple[date, float]] = {}
    for row in rows:
        if row.trade_date > as_of_date or row.close is None or row.close <= 0:
            continue
        previous = latest.get(row.stock_code)
        if previous is None or row.trade_date > previous[0]:
            latest[row.stock_code] = (row.trade_date, float(row.close))
    return {stock_code: value for stock_code, (_, value) in latest.items()}
