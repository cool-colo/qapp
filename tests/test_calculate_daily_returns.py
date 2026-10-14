from argparse import Namespace
from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from scripts.calculate_daily_returns import calculate_records
from scripts.calculate_daily_returns import calculate_records_by_account
from scripts.calculate_daily_returns import fetch_mysql_inputs
from scripts.calculate_daily_returns import main


class DailyReturnCalculationTests(unittest.TestCase):
    def _asset(self, market_value: str, cash: str, frozen_cash: str) -> dict:
        return {
            "account_id": "account", "trader_id": "trader",
            "market_value": market_value, "cash": cash, "frozen_cash": frozen_cash,
            # The summary must use the requested components, not total_asset.
            "total_asset": "999999",
        }

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

    def test_summary_uses_net_value_change_and_previous_net_value_as_reference(self) -> None:
        position = {
            "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
        }
        rows = calculate_records(
            "2026-07-27", trades=[], before_positions=[position], after_positions=[position],
            ticks=[{"stock_code": "600000.SH", "last_price": "11", "last_close": "10"}],
            current_asset=self._asset("1100", "8850", "200"),
            previous_asset=self._asset("1000", "8900", "100"),
        )
        stock_total = next(row for row in rows if row["instrument_id"] != "summary" and row["calculation_type"] == "all")
        self.assertEqual(stock_total["return_amount"], Decimal("100.0000"))
        self.assertEqual(stock_total["reference_amount"], Decimal("1000.0000"))
        summary = rows[-1]
        self.assertEqual(summary["stock_code"], "summary")
        self.assertEqual(summary["return_amount"], Decimal("150.0000"))
        self.assertEqual(summary["reference_amount"], Decimal("10000.0000"))
        self.assertEqual(summary["return_rate"], Decimal("0.01500000"))

    def test_summary_falls_back_when_either_asset_day_is_missing(self) -> None:
        position = {
            "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
        }
        asset = self._asset("1000", "9000", "0")
        for current, previous in ((asset, None), (None, asset), (None, None)):
            with self.subTest(current=current, previous=previous):
                rows = calculate_records(
                    "2026-07-27", trades=[], before_positions=[position], after_positions=[position],
                    ticks=[{"stock_code": "600000.SH", "last_price": "11", "last_close": "10"}],
                    current_asset=current, previous_asset=previous,
                )
                summary = rows[-1]
                self.assertEqual(summary["return_amount"], Decimal("100.0000"))
                self.assertEqual(summary["reference_amount"], Decimal("1000.0000"))
                self.assertEqual(summary["return_rate"], Decimal("0.10000000"))

    def test_asset_only_accounts_produce_summary_for_losses_flat_and_zero_base(self) -> None:
        for previous_cash, current_cash, expected_pnl, expected_rate in (
            ("10000", "9900", "-100.0000", Decimal("-0.01000000")),
            ("10000", "10000", "0.0000", Decimal("0.00000000")),
            ("0", "100", "100.0000", None),
        ):
            with self.subTest(previous_cash=previous_cash, current_cash=current_cash):
                records, failures = calculate_records_by_account(
                    "2026-07-27", [], [], [], [],
                    current_assets=[self._asset("0", current_cash, "0")],
                    previous_assets=[self._asset("0", previous_cash, "0")],
                )
                self.assertEqual(failures, {})
                rows = records[("account", "trader")]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["instrument_id"], "summary")
                self.assertEqual(rows[0]["return_amount"], Decimal(expected_pnl))
                self.assertEqual(rows[0]["reference_amount"], Decimal(previous_cash))
                self.assertEqual(rows[0]["return_rate"], expected_rate)

    def test_invalid_asset_snapshots_fail_only_the_affected_trader(self) -> None:
        asset = self._asset("1000", "9000", "0")
        corrupt = {**asset, "trader_id": "corrupt", "frozen_cash": None}
        records, failures = calculate_records_by_account(
            "2026-07-27", [], [], [], [],
            current_assets=[self._asset("1100", "9000", "0"), corrupt],
            previous_assets=[asset, {**asset, "trader_id": "corrupt"}],
        )
        self.assertEqual(set(records), {("account", "trader")})
        self.assertEqual(records[("account", "trader")][0]["return_amount"], Decimal("100.0000"))
        self.assertEqual(set(failures), {("account", "corrupt")})
        self.assertIn("missing frozen_cash", str(failures[("account", "corrupt")]))

    def test_missing_asset_day_for_one_account_does_not_affect_another(self) -> None:
        positions = [
            {
                "account_id": account_id, "trader_id": "trader", "instrument_id": "600000.SH",
                "stock_code": "600000.SH", "volume": 100,
            }
            for account_id in ("account", "fallback")
        ]
        records, failures = calculate_records_by_account(
            "2026-07-27", [], positions, positions,
            [{"stock_code": "600000.SH", "last_price": "11", "last_close": "10"}],
            current_assets=[
                self._asset("1100", "9000", "0"),
                {**self._asset("1100", "9000", "0"), "account_id": "fallback"},
            ],
            previous_assets=[self._asset("1000", "8000", "0")],
        )
        self.assertEqual(failures, {})
        self.assertEqual(records[("account", "trader")][-1]["return_amount"], Decimal("1100.0000"))
        self.assertEqual(records[("fallback", "trader")][-1]["return_amount"], Decimal("100.0000"))
        self.assertEqual(records[("fallback", "trader")][-1]["reference_amount"], Decimal("1000.0000"))

    def test_fetches_after_trading_assets_for_current_and_exact_previous_trading_day(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        previous_trade_date = date(2026, 7, 24)  # Friday before the requested Monday.
        cursor.fetchone.return_value = {"pretrade_date": previous_trade_date}
        current_assets = [self._asset("1100", "9000", "0")]
        previous_assets = [self._asset("1000", "9000", "0")]
        cursor.fetchall.side_effect = [[], [], [], [], [], current_assets, previous_assets]

        inputs = fetch_mysql_inputs(
            connection, Namespace(account_id="account", trader_id="trader"), "2026-07-27",
        )

        self.assertEqual(inputs, ([], [], [], [], current_assets, previous_assets))
        calendar_queries = [call for call in cursor.execute.call_args_list if "`trade_calendar`" in call.args[0]]
        self.assertEqual(len(calendar_queries), 1)
        self.assertEqual(calendar_queries[0].args[1], ("2026-07-27",))
        asset_queries = [call for call in cursor.execute.call_args_list if "`live_asset_snapshot`" in call.args[0]]
        self.assertEqual(len(asset_queries), 2)
        for query, day in zip(asset_queries, ("2026-07-27", previous_trade_date)):
            sql, params = query.args
            self.assertIn("`market_value`, `cash`, `frozen_cash`", sql)
            self.assertIn("`snapshot_type` = 'after_trading'", sql)
            self.assertIn("`trade_date` = %s", sql)
            self.assertIn("`account_id` = %s AND `trader_id` = %s", sql)
            self.assertEqual(params, [day, "account", "trader"])

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
            [{**self._asset("1100", "9000", "0"), "account_id": "good"}],
            [{**self._asset("1000", "9000", "0"), "account_id": "good"}],
        )

        result = main(["--trade-date", "2026-07-27", "--no-create-table"])

        self.assertEqual(result, 1)
        upsert.assert_called_once()
        written_rows = upsert.call_args.args[2]
        self.assertEqual({row["account_id"] for row in written_rows}, {"good"})
        summary = written_rows[-1]
        self.assertEqual(summary["return_amount"], Decimal("100.0000"))
        self.assertEqual(summary["reference_amount"], Decimal("10000.0000"))
        self.assertEqual(summary["return_rate"], Decimal("0.01000000"))
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
