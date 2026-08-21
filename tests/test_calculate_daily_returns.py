from decimal import Decimal
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from scripts.calculate_daily_returns import calculate_records
from scripts.calculate_daily_returns import calculate_records_by_account
from scripts.calculate_daily_returns import main


class DailyReturnCalculationTests(unittest.TestCase):
    def test_calculates_buy_sell_hold_stock_total_and_summary(self) -> None:
        rows = calculate_records(
            "2026-07-27",
            trades=[
                {
                    "account_id": "account",
                    "trader_id": "trader",
                    "instrument_id": "600000.SSE",
                    "stock_code": "600000.SH",
                    "side": "BUY",
                    "price": "10",
                    "quantity": 100,
                    "amount": "1000",
                },
                {
                    "account_id": "account",
                    "trader_id": "trader",
                    "instrument_id": "600000.SSE",
                    "stock_code": "600000.SH",
                    "side": "SELL",
                    "price": "11",
                    "quantity": 200,
                    "amount": "2200",
                },
            ],
            before_positions=[
                {
                    "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SSE",
                    "stock_code": "600000.SH", "volume": 500, "avg_price": "9",
                },
            ],
            after_positions=[
                {
                    "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SSE",
                    "stock_code": "600000.SH", "volume": 400, "avg_price": "9.2",
                },
            ],
            ticks=[{
                "instrument_id": "600000.SSE", "stock_code": "600000.SH",
                "last_price": "10.5", "open": "10.1", "last_close": "10",
            }],
        )
        by_type = {row["calculation_type"]: row for row in rows if row["stock_code"] != "summary"}
        self.assertEqual(set(by_type), {"buy", "sell", "hold", "all"})
        self.assertEqual(by_type["buy"]["return_amount"], Decimal("45.0000"))
        self.assertEqual(by_type["sell"]["tax"], Decimal("0.1100"))
        self.assertEqual(by_type["sell"]["return_amount"], Decimal("194.8900"))
        self.assertEqual(by_type["hold"]["quantity"], 300)
        self.assertEqual(by_type["hold"]["return_amount"], Decimal("150.0000"))
        self.assertEqual(by_type["all"]["return_amount"], Decimal("389.8900"))
        summary = next(row for row in rows if row["stock_code"] == "summary")
        self.assertEqual(summary["calculation_type"], "all")
        self.assertEqual(summary["return_amount"], Decimal("389.8900"))

    def test_rejects_unreconciled_opening_and_closing_positions(self) -> None:
        with self.assertRaisesRegex(ValueError, "position reconciliation failed"):
            calculate_records(
                "2026-07-27",
                trades=[],
                before_positions=[
                    {"account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ", "stock_code": "000001.SZ", "volume": 100},
                ],
                after_positions=[
                    {"account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ", "stock_code": "000001.SZ", "volume": 200},
                ],
                ticks=[{"instrument_id": "000001.SZ", "last_price": "10", "last_close": "9"}],
            )

    def test_uses_tick_last_close_for_fully_sold_stock(self) -> None:
        rows = calculate_records(
            "2026-07-27",
            trades=[
                {
                    "account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ",
                    "stock_code": "000001.SZ", "side": "SELL", "price": "10",
                    "quantity": 100, "amount": "1000",
                },
            ],
            before_positions=[
                {
                    "account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ",
                    "stock_code": "000001.SZ", "volume": 100,
                },
            ],
            after_positions=[],
            ticks=[{"instrument_id": "000001.SZ", "last_close": "9"}],
        )
        sell = next(row for row in rows if row["calculation_type"] == "sell")
        self.assertEqual(sell["close_price"], None)
        self.assertEqual(sell["pre_close"], Decimal("9"))
        self.assertEqual(sell["return_amount"], Decimal("94.9500"))

    def test_ignores_zero_volume_snapshot_without_trades(self) -> None:
        self.assertEqual(
            calculate_records(
                "2026-07-27",
                trades=[],
                before_positions=[
                    {
                        "account_id": "a", "trader_id": "t", "instrument_id": "000720.SZ.QMT",
                        "stock_code": "000720.SZ", "volume": 0,
                    },
                ],
                after_positions=[],
                ticks=[],
            ),
            [],
        )

    def test_calculates_accounts_independently_when_one_is_corrupt(self) -> None:
        positions = [
            {
                "account_id": account_id,
                "trader_id": "trader",
                "instrument_id": "000001.SZ",
                "stock_code": "000001.SZ",
                "volume": volume,
            }
            for account_id, volume in (("good", 100), ("corrupt", 200))
        ]
        records, failures = calculate_records_by_account(
            "2026-07-27",
            trades=[],
            before_positions=positions,
            after_positions=[
                {**row, "volume": 100}
                for row in positions
            ],
            ticks=[{
                "instrument_id": "000001.SZ",
                "stock_code": "000001.SZ",
                "last_price": "10",
                "last_close": "9",
            }],
        )

        self.assertEqual(set(records), {("good", "trader")})
        self.assertEqual(set(failures), {("corrupt", "trader")})
        self.assertIn("position reconciliation failed", str(failures[("corrupt", "trader")]))
        self.assertEqual(records[("good", "trader")][-1]["stock_code"], "summary")

    @patch("scripts.calculate_daily_returns.load_env")
    @patch("scripts.calculate_daily_returns.upsert_records")
    @patch("scripts.calculate_daily_returns.fetch_mysql_inputs")
    @patch("scripts.calculate_daily_returns._connect_mysql")
    def test_main_persists_valid_account_when_another_account_is_corrupt(
        self,
        connect_mysql: MagicMock,
        fetch_inputs: MagicMock,
        upsert: MagicMock,
        _load_env: MagicMock,
    ) -> None:
        connection = connect_mysql.return_value
        positions = [
            {
                "account_id": account_id,
                "trader_id": "trader",
                "instrument_id": "000001.SZ",
                "stock_code": "000001.SZ",
                "volume": volume,
            }
            for account_id, volume in (("good", 100), ("corrupt", 200))
        ]
        fetch_inputs.return_value = (
            [],
            positions,
            [{**row, "volume": 100} for row in positions],
            [{
                "instrument_id": "000001.SZ",
                "stock_code": "000001.SZ",
                "last_price": "10",
                "last_close": "9",
            }],
        )

        result = main(["--trade-date", "2026-07-27", "--no-create-table"])

        self.assertEqual(result, 1)
        upsert.assert_called_once()
        written_rows = upsert.call_args.args[2]
        self.assertEqual({row["account_id"] for row in written_rows}, {"good"})
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
