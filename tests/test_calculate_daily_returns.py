from decimal import Decimal
import unittest

from scripts.calculate_daily_returns import calculate_records


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


if __name__ == "__main__":
    unittest.main()
