from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from backtests.data_providers.clickhouse import ClickHouseBarDataProvider
from backtests.data_providers.clickhouse import ClickHouseBarSchema
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.clickhouse import ensure_json_each_row
from backtests.data_providers.clickhouse import quote_identifier
from backtests.data_providers.clickhouse import quote_literal
from backtests.data_providers.clickhouse_model_predictions import normalize_stock_code
from market_data import DailyStockData
from market_data import index_daily_stock_data


class ClickHouseDailyStockDataProvider:
    """Load reusable daily OHLC and authoritative exchange price limits."""

    def __init__(self, connection: ClickHouseConnectionConfig) -> None:
        self._client = ClickHouseBarDataProvider(connection, ClickHouseBarSchema())

    def load(
        self,
        stock_codes: list[str],
        start_date: date | str,
        end_date: date | str,
    ) -> tuple[DailyStockData, ...]:
        start = pd.Timestamp(start_date).date()
        end = pd.Timestamp(end_date).date()
        if end < start:
            raise ValueError(f"Daily stock data end_date {end} precedes start_date {start}")
        normalized_codes = sorted(
            {
                normalized
                for code in stock_codes
                if (normalized := normalize_stock_code(code)) is not None
            },
        )
        if not normalized_codes:
            return ()

        rows: list[DailyStockData] = []
        for offset in range(0, len(normalized_codes), 500):
            chunk = normalized_codes[offset : offset + 500]
            raw_rows = self._client.fetch_json_each_row(self._query(chunk, start, end))
            rows.extend(self._parse_row(row) for row in raw_rows)

        rows.sort(key=lambda row: (row.trade_date, row.stock_code))
        index_daily_stock_data(rows)
        return tuple(rows)

    @staticmethod
    def _query(stock_codes: list[str], start_date: date, end_date: date) -> str:
        values = ", ".join(quote_literal(code) for code in stock_codes)
        factor_table = quote_identifier("dws_stock_factor_wide")
        limit_table = quote_identifier("dwd_stock_limit")
        sql = f"""
SELECT
    factor.source_code AS stock_code,
    factor.trade_date AS trade_date,
    factor.open AS open,
    factor.high AS high,
    factor.low AS low,
    factor.close AS close,
    limits.pre_close AS pre_close,
    limits.up_limit AS up_limit,
    limits.down_limit AS down_limit
FROM {factor_table} AS factor
LEFT JOIN
(
    SELECT source_code, trade_date, pre_close, up_limit, down_limit
    FROM {limit_table}
    WHERE instrument_type = 'stock'
      AND sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
      AND source_code IN ({values})
      AND trade_date >= {quote_literal(str(start_date))}
      AND trade_date <= {quote_literal(str(end_date))}
) AS limits
ON factor.source_code = limits.source_code
AND factor.trade_date = limits.trade_date
WHERE factor.source_code IN ({values})
  AND factor.trade_date >= {quote_literal(str(start_date))}
  AND factor.trade_date <= {quote_literal(str(end_date))}
ORDER BY trade_date, stock_code
"""
        return ensure_json_each_row(sql)

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> DailyStockData:
        stock_code = normalize_stock_code(row.get("stock_code"))
        if stock_code is None:
            raise ValueError(f"Daily stock data row has invalid stock_code: {row!r}")
        raw_date = row.get("trade_date")
        if raw_date in (None, ""):
            raise ValueError(f"Daily stock data row has no trade_date: {row!r}")
        return DailyStockData(
            stock_code=stock_code,
            trade_date=pd.Timestamp(raw_date).date(),
            open=_optional_float(row.get("open")),
            high=_optional_float(row.get("high")),
            low=_optional_float(row.get("low")),
            close=_optional_float(row.get("close")),
            pre_close=_optional_float(row.get("pre_close")),
            up_limit=_optional_float(row.get("up_limit")),
            down_limit=_optional_float(row.get("down_limit")),
        )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid daily stock numeric value: {value!r}") from exc
