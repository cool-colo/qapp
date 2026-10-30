from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scripts.calculate_live_trade_costs_slippage import _load_mysql_exports
from scripts.calculate_live_trade_costs_slippage import calculate_grouped_report
from scripts.calculate_live_trade_costs_slippage import write_report


class CalculateLiveTradeCostsSlippageTests(unittest.TestCase):
    def test_groups_fees_and_quantity_weighted_slippage(self) -> None:
        trades = [
            self._trade("buy-1", "BUY", "11", 10, client_order_id="client-1"),
            self._trade("buy-2", "buy", "13", 30, venue_order_id="venue-2"),
            self._trade("sell-1", "SELL", "9", 20, instrument_id="000002.SZ.BIGQMT"),
        ]
        orders = [
            self._order("10", client_order_id="client-1"),
            self._order("12", client_order_id="", venue_order_id="venue-2"),
        ]
        ticks = [
            {
                "trade_date": date(2026, 10, 29),
                "instrument_id": "000002.SZ.BIGQMT",
                "min_open": Decimal("10"),
                "max_open": Decimal("10"),
            },
        ]

        rows = calculate_grouped_report(trades, orders, ticks)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["buy_turnover"], Decimal("500.0000"))
        self.assertEqual(row["sell_turnover"], Decimal("180.0000"))
        self.assertEqual(row["commission_fee"], Decimal("0.0595"))
        self.assertEqual(row["stamp_tax"], Decimal("0.0900"))
        self.assertEqual(row["transfer_fee"], Decimal("0.0068"))
        self.assertEqual(row["total_trading_fee"], Decimal("0.1563"))
        self.assertEqual(row["buy_weighted_fill_price"], Decimal("12.500000"))
        self.assertEqual(row["buy_weighted_open_price"], Decimal("11.500000"))
        self.assertEqual(row["buy_slippage"], Decimal("1.000000"))
        self.assertEqual(row["buy_slippage_bps"], Decimal("800.000000"))
        # The sell filled below the open, so the unified adverse-slippage sign is positive.
        self.assertEqual(row["sell_slippage"], Decimal("1.000000"))
        self.assertEqual(row["sell_slippage_bps"], Decimal("1111.111111"))

    def test_missing_open_is_counted_but_trade_stays_in_fee_calculation(self) -> None:
        trades = [self._trade("buy-1", "BUY", "10", 100)]

        row = calculate_grouped_report(trades, [], [])[0]

        self.assertEqual(row["buy_turnover"], Decimal("1000.0000"))
        self.assertEqual(row["buy_missing_open_trade_count"], 1)
        self.assertEqual(row["buy_slippage_trade_count"], 0)
        self.assertIsNone(row["buy_slippage"])
        self.assertIsNone(row["buy_slippage_bps"])

    def test_launcher_parser_loads_only_allowlisted_mysql_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "start.sh"
            launcher.write_text(
                "export MYSQL_HOST=db.example\n"
                "export MYSQL_PORT=3307\n"
                "export MYSQL_PASSWORD='secret-value'\n"
                "export BIGQMT_REDIS_PASSWORD=must-not-load\n"
                "exec python something.py\n",
                encoding="utf-8",
            )
            clean_environment = {key: value for key, value in os.environ.items() if key not in {
                "MYSQL_HOST", "MYSQL_PORT", "MYSQL_PASSWORD", "BIGQMT_REDIS_PASSWORD",
            }}
            with patch.dict(os.environ, clean_environment, clear=True):
                _load_mysql_exports(launcher)
                self.assertEqual(os.environ["MYSQL_HOST"], "db.example")
                self.assertEqual(os.environ["MYSQL_PORT"], "3307")
                self.assertEqual(os.environ["MYSQL_PASSWORD"], "secret-value")
                self.assertNotIn("BIGQMT_REDIS_PASSWORD", os.environ)

    def test_writes_csv_report_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.csv"

            result = write_report(
                [{"account_id": "account", "trader_id": "trader", "trade_count": 1}],
                "csv",
                output,
            )

            self.assertEqual(result, output)
            content = output.read_text(encoding="utf-8")
            self.assertIn("account_id,trader_id,trade_count", content)
            self.assertIn("account,trader,1", content)

    @staticmethod
    def _trade(
        trade_id: str,
        side: str,
        price: str,
        quantity: int,
        *,
        client_order_id: str = "",
        venue_order_id: str = "",
        instrument_id: str = "000001.SZ.BIGQMT",
    ) -> dict[str, object]:
        return {
            "trade_date": date(2026, 10, 29),
            "account_id": "account",
            "trader_id": "trader",
            "trade_id": trade_id,
            "client_order_id": client_order_id,
            "venue_order_id": venue_order_id,
            "instrument_id": instrument_id,
            "side": side,
            "price": Decimal(price),
            "quantity": quantity,
        }

    @staticmethod
    def _order(
        open_price: str,
        *,
        client_order_id: str,
        venue_order_id: str = "",
    ) -> dict[str, object]:
        return {
            "trade_date": date(2026, 10, 29),
            "account_id": "account",
            "trader_id": "trader",
            "client_order_id": client_order_id,
            "venue_order_id": venue_order_id,
            "open_price": Decimal(open_price),
        }


if __name__ == "__main__":
    unittest.main()
