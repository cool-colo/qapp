from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

from backtests.data_providers.clickhouse_daily_stock import ClickHouseDailyStockDataProvider
from market_data import DailyStockData
from market_data import latest_closes


class ClickHouseDailyStockDataProviderTest(unittest.TestCase):
    def _provider(self, rows: list[dict]) -> ClickHouseDailyStockDataProvider:
        provider = ClickHouseDailyStockDataProvider.__new__(ClickHouseDailyStockDataProvider)
        provider._client = MagicMock()
        provider._client.fetch_json_each_row.return_value = rows
        return provider

    def test_loads_typed_daily_rows_and_active_limit_join(self) -> None:
        provider = self._provider(
            [
                {
                    "stock_code": "000001.SZ",
                    "trade_date": "2026-07-07",
                    "open": "10.1",
                    "high": "11",
                    "low": "10",
                    "close": "11",
                    "pre_close": "10",
                    "up_limit": "11",
                    "down_limit": "9",
                },
            ],
        )

        rows = provider.load(["000001.SZ"], date(2026, 7, 7), date(2026, 7, 8))

        self.assertEqual(rows[0].close, 11.0)
        self.assertEqual(rows[0].pre_close, 10.0)
        sql = provider._client.fetch_json_each_row.call_args.args[0]
        self.assertIn("dws_stock_factor_wide", sql)
        self.assertIn("dwd_stock_limit", sql)
        self.assertIn("2299-12-31 00:00:00.000", sql)
        self.assertIn("FORMAT JSONEachRow", sql)

    def test_duplicate_stock_date_fails_fast(self) -> None:
        row = {
            "stock_code": "000001.SZ",
            "trade_date": "2026-07-07",
            "close": 11,
            "up_limit": 11,
        }
        provider = self._provider([row, row])

        with self.assertRaisesRegex(ValueError, "Duplicate daily stock data"):
            provider.load(["000001.SZ"], date(2026, 7, 7), date(2026, 7, 8))

    def test_latest_closes_uses_latest_row_on_or_before_date(self) -> None:
        rows = (
            DailyStockData("000001.SZ", date(2026, 7, 7), close=10.0),
            DailyStockData("000001.SZ", date(2026, 7, 8), close=11.0),
            DailyStockData("000001.SZ", date(2026, 7, 9), close=12.0),
        )

        self.assertEqual(latest_closes(rows, date(2026, 7, 8)), {"000001.SZ": 11.0})


if __name__ == "__main__":
    unittest.main()
