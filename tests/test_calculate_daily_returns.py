from argparse import Namespace
from datetime import date, datetime
from decimal import Decimal
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from scripts.calculate_daily_returns import calculate_records
from scripts.calculate_daily_returns import calculate_records_by_account
from scripts.calculate_daily_returns import fetch_mysql_inputs
from scripts.calculate_daily_returns import main
from scripts.calculate_daily_returns import reconstruct_opening_quantity


class DailyReturnCalculationTests(unittest.TestCase):
    def _position_snapshot(self, volume: int, stock_code: str = "001210.SZ") -> dict:
        return {
            "account_id": "8886569186", "trader_id": "BIGQMT-PROD-001",
            "instrument_id": stock_code + ".BIGQMT", "stock_code": stock_code,
            "volume": volume, "avg_price": "10", "trade_date": "2026-11-19",
            "write_time": datetime(2026, 11, 19, 9, 27),
        }

    def _asset(
        self, market_value: str, cash: str, frozen_cash: str,
        *, account_id: str = "account", trade_date: date | str = "2026-07-27",
    ) -> dict:
        return {
            "account_id": account_id, "trader_id": "trader", "trade_date": trade_date,
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
                    "trade_time": "2026-07-27 10:00:00",
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
                    "trade_time": "2026-07-27 10:00:00",
                },
            ],
            before_positions=[
                {
                    "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SSE",
                    "stock_code": "600000.SH", "volume": 500, "avg_price": "9",
                    "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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

    def test_reconstructs_opening_positions_and_counts_all_day_fills_once(self) -> None:
        for label, snapshot_qty, fills, after_qty, expected_opening, expected_hold, expected_pnl in (
            ("new auction buy", 1100, [("buy", 1100, "09:25:00")], 1100, 0, 0, "1095"),
            ("auction and later buys", 1500, [("buy", 500, "09:25:00"), ("buy", 200, "10:00:00")], 1700, 1000, 1000, "1695"),
            ("auction and later sells", 600, [("sell", 400, "09:25:00"), ("sell", 200, "10:00:00")], 400, 1000, 400, "994.67"),
            ("fully sold at auction", 0, [("sell", 1000, "09:25:00")], 0, 1000, 0, "994.45"),
            ("credited bonus shares", 1500, [("buy", 100, "10:00:00")], 1600, 1500, 1500, "1595"),
            ("fill exactly at snapshot", 1100, [("buy", 100, "09:27:00")], 1100, 1000, 1000, "1095"),
        ):
            with self.subTest(label=label):
                snapshot = self._position_snapshot(snapshot_qty)
                trades = [
                    {
                        "account_id": snapshot["account_id"], "trader_id": snapshot["trader_id"],
                        "instrument_id": snapshot["instrument_id"], "stock_code": snapshot["stock_code"],
                        "side": side, "quantity": quantity, "price": "10" if side == "buy" else "11",
                        "trade_time": "2026-11-19 " + fill_time,
                    }
                    for side, quantity, fill_time in fills
                ]
                self.assertEqual(reconstruct_opening_quantity("2026-11-19", snapshot, trades), expected_opening)
                rows = calculate_records(
                    "2026-11-19", trades, [snapshot], [{**snapshot, "volume": after_qty}] if after_qty else [],
                    [{"stock_code": snapshot["stock_code"], "last_price": "11", "last_close": "10"}],
                )
                self.assertEqual(sum(row["quantity"] for row in rows if row["calculation_type"] == "hold"), expected_hold)
                self.assertEqual(rows[-1]["return_amount"], Decimal(expected_pnl))
                self.assertEqual(snapshot["volume"], snapshot_qty)

    def test_reconstruction_uses_shanghai_time_for_aware_timestamps(self) -> None:
        snapshot = self._position_snapshot(1100)
        snapshot["write_time"] = "2026-11-19T09:27:00+08:00"
        trades = [{"quantity": 1100, "side": "BUY", "trade_time": "2026-11-19T01:25:00+00:00"}]
        self.assertEqual(reconstruct_opening_quantity("2026-11-19", snapshot, trades), 0)

    def test_invalid_reconstructed_positions_fail_only_the_affected_account(self) -> None:
        for label, volume, trade_time, expected_error in (
            ("missing fill time", 100, None, "missing trade_time"),
            ("wrong fill day", 100, "2026-11-18 09:25:00", "trade_time is not on"),
            ("negative opening volume", 50, "2026-11-19 09:25:00", "negative reconstructed opening quantity"),
        ):
            with self.subTest(label=label):
                corrupt = self._position_snapshot(volume)
                good = {**self._position_snapshot(100), "account_id": "good"}
                records, failures = calculate_records_by_account(
                    "2026-11-19",
                    [{**corrupt, "side": "buy", "quantity": 100, "price": "10", "trade_time": trade_time}],
                    [good, corrupt], [good, corrupt],
                    [{"stock_code": corrupt["stock_code"], "last_price": "11", "last_close": "10"}],
                )
                self.assertEqual(set(records), {("good", "BIGQMT-PROD-001")})
                self.assertEqual(set(failures), {("8886569186", "BIGQMT-PROD-001")})
                self.assertIn(expected_error, str(next(iter(failures.values()))))

    def test_fetch_reconstructs_missing_fully_sold_stock_and_preserves_bonus_shares(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"pretrade_date": date(2026, 11, 18)}
        bonus = self._position_snapshot(1500, "002734.SZ")
        sold = self._position_snapshot(0)
        previous = [
            {**row, "volume": quantity, "trade_date": date(2026, 11, 18), "write_time": "2026-11-18 15:03:00"}
            for row, quantity in ((bonus, 1000), (sold, 100))
        ]
        # The sold stock also received bonus shares and was fully sold before
        # 09:27, so yesterday's 100 shares must not override today's zero row.
        trades = [{**sold, "quantity": 200, "side": "sell", "price": "11", "trade_time": "2026-11-19 09:25:00"}]
        ticks = [{"stock_code": row["stock_code"], "last_price": "11", "last_close": "10"} for row in (bonus, sold)]
        header = {key: sold[key] for key in ("account_id", "trader_id", "trade_date", "write_time")}
        cursor.fetchall.side_effect = [[bonus], previous, [bonus], trades, ticks, [], [], [header]]
        inputs = fetch_mysql_inputs(
            connection, Namespace(account_id=sold["account_id"], trader_id=sold["trader_id"]), "2026-11-19",
        )
        before = {row["instrument_id"]: row for row in inputs[1]}
        self.assertEqual(before[sold["instrument_id"]]["volume"], 0)
        self.assertEqual(reconstruct_opening_quantity("2026-11-19", before[sold["instrument_id"]], trades), 200)
        self.assertEqual(before[bonus["instrument_id"]]["volume"], 1500)
        rows = calculate_records("2026-11-19", *inputs[:4])
        self.assertEqual(rows[-1]["return_amount"], Decimal("1694.89"))
        trade_query = next(call.args[0] for call in cursor.execute.call_args_list if "FROM `live_trade`" in call.args[0])
        self.assertIn("`trade_time`", trade_query)

    def test_fetch_preserves_previous_day_baseline_when_account_has_no_current_snapshot(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"pretrade_date": date(2026, 11, 18)}
        previous = {**self._position_snapshot(100), "trade_date": "2026-11-18", "write_time": "2026-11-18 15:03:00"}
        current = self._position_snapshot(200)
        trades = [{**current, "side": "buy", "quantity": 100, "price": "10", "trade_time": "2026-11-19 09:25:00"}]
        ticks = [{"stock_code": current["stock_code"], "last_price": "11", "last_close": "10"}]
        cursor.fetchall.side_effect = [[], [previous], [current], trades, ticks, [], [], []]
        inputs = fetch_mysql_inputs(
            connection, Namespace(account_id=current["account_id"], trader_id=current["trader_id"]), "2026-11-19",
        )
        self.assertEqual(inputs[1], [previous])
        self.assertEqual(reconstruct_opening_quantity("2026-11-19", previous, trades), 100)
        rows = calculate_records("2026-11-19", *inputs[:4])
        self.assertEqual(rows[-1]["return_amount"], Decimal("195"))

    def test_summary_uses_net_value_change_and_previous_net_value_as_reference(self) -> None:
        position = {
            "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
            "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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

    def test_cash_deposit_is_excluded_from_net_value_on_and_after_effective_date(self) -> None:
        for previous_day, current_day, previous_cash, current_cash, expected_pnl, expected_base, expected_rate in (
            ("2026-11-17", "2026-11-18", "990000", "1000000", "10000", "990000", "0.01010101"),
            ("2026-11-18", "2026-11-19", "1000000", "1160000", "10000", "1000000", "0.01000000"),
            ("2026-11-18", "2026-11-19", "1000000", "1150000", "0", "1000000", "0.00000000"),
            ("2026-11-19", "2026-11-20", "1160000", "1155000", "-5000", "1010000", "-0.00495050"),
            ("2026-11-19", "2026-11-20", "1150000", "1150000", "0", "1000000", "0.00000000"),
            ("2026-11-20", "2026-11-23", "1155000", "1160000", "5000", "1005000", "0.00497512"),
        ):
            with self.subTest(day=current_day, current_cash=current_cash):
                current = self._asset("0", current_cash, "0", account_id="8886569186", trade_date=current_day)
                previous = self._asset("0", previous_cash, "0", account_id="8886569186", trade_date=date.fromisoformat(previous_day))
                records, failures = calculate_records_by_account(
                    current_day, [], [], [], [], current_assets=[current], previous_assets=[previous],
                )
                self.assertEqual(failures, {})
                summary = records[("8886569186", "trader")][-1]
                self.assertEqual(summary["return_amount"], Decimal(expected_pnl))
                self.assertEqual(summary["reference_amount"], Decimal(expected_base))
                self.assertEqual(summary["return_rate"], Decimal(expected_rate))
                # Reporting adjustments must not mutate the broker snapshots.
                self.assertEqual(current["cash"], current_cash)
                self.assertEqual(previous["cash"], previous_cash)

    def test_cash_deposit_does_not_adjust_other_accounts(self) -> None:
        records, failures = calculate_records_by_account(
            "2026-11-19", [], [], [], [],
            current_assets=[
                self._asset("0", "1160000", "0", account_id=account_id, trade_date="2026-11-19")
                for account_id in ("8886569186", "other")
            ],
            previous_assets=[
                self._asset("0", "1000000", "0", account_id=account_id, trade_date="2026-11-18")
                for account_id in ("8886569186", "other")
            ],
        )
        self.assertEqual(failures, {})
        self.assertEqual(records[("8886569186", "trader")][-1]["return_amount"], Decimal("10000"))
        self.assertEqual(records[("other", "trader")][-1]["return_amount"], Decimal("160000"))

    def test_cash_deposit_does_not_change_stock_pnl_or_positions_and_trades_summary(self) -> None:
        position = {
            "account_id": "8886569186", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
            "trade_date": "2026-11-19", "write_time": "2026-11-19 09:27:00",
        }
        for method, expected_reference in (("asset-snapshot", "1000000"), ("positions-and-trades", "1000")):
            with self.subTest(method=method):
                rows = calculate_records(
                    "2026-11-19", [], [position], [position],
                    [{"stock_code": "600000.SH", "last_price": "11", "last_close": "10"}],
                    current_asset=self._asset("1100", "1149000", "0", account_id="8886569186", trade_date="2026-11-19"),
                    previous_asset=self._asset("1000", "999000", "0", account_id="8886569186", trade_date="2026-11-18"),
                    account_return_method=method,
                )
                self.assertEqual(rows[-1]["return_amount"], Decimal("100"))
                self.assertEqual(rows[-1]["reference_amount"], Decimal(expected_reference))
                stock_total = next(row for row in rows if row["stock_code"] != "summary" and row["calculation_type"] == "all")
                self.assertEqual(stock_total["return_amount"], Decimal("100"))
                self.assertEqual(stock_total["reference_amount"], Decimal("1000"))

    def test_summary_can_be_forced_to_use_positions_and_trades(self) -> None:
        position = {
            "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
            "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
        }
        rows = calculate_records(
            "2026-07-27", trades=[], before_positions=[position], after_positions=[position],
            ticks=[{"stock_code": "600000.SH", "last_price": "11", "last_close": "10"}],
            current_asset=self._asset("1100", "8850", "200"),
            previous_asset=self._asset("1000", "8900", "100"),
            account_return_method="positions-and-trades",
        )

        summary = rows[-1]
        self.assertEqual(summary["stock_code"], "summary")
        self.assertEqual(summary["return_amount"], Decimal("100.0000"))
        self.assertEqual(summary["reference_amount"], Decimal("1000.0000"))
        self.assertEqual(summary["return_rate"], Decimal("0.10000000"))

    @patch("scripts.calculate_daily_returns.load_env")
    @patch("scripts.calculate_daily_returns.calculate_records_by_account", return_value=({}, {}))
    @patch("scripts.calculate_daily_returns.fetch_mysql_inputs", return_value=([], [], [], [], [], []))
    @patch("scripts.calculate_daily_returns._connect_mysql")
    def test_main_passes_configured_account_return_method(
        self,
        connect_mysql: MagicMock,
        _fetch_inputs: MagicMock,
        calculate_by_account: MagicMock,
        _load_env: MagicMock,
    ) -> None:
        result = main([
            "--trade-date", "2026-07-27",
            "--no-create-table",
            "--account-return-method", "positions-and-trades",
        ])

        self.assertEqual(result, 0)
        self.assertEqual(
            calculate_by_account.call_args.kwargs["account_return_method"],
            "positions-and-trades",
        )
        connect_mysql.return_value.close.assert_called_once_with()

    def test_summary_falls_back_when_either_asset_day_is_missing(self) -> None:
        position = {
            "account_id": "account", "trader_id": "trader", "instrument_id": "600000.SH",
            "stock_code": "600000.SH", "volume": 100,
            "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
                "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
        current_assets = [self._asset("1100", "9000", "0", trade_date="2026-07-27")]
        previous_assets = [self._asset("1000", "9000", "0", trade_date=previous_trade_date)]
        cursor.fetchall.side_effect = [[], [], [], [], [], current_assets, previous_assets, []]

        inputs = fetch_mysql_inputs(
            connection, Namespace(account_id="account", trader_id="trader"), "2026-07-27",
        )

        self.assertEqual(inputs, ([], [], [], [], current_assets, previous_assets))
        calendar_queries = [call for call in cursor.execute.call_args_list if "`trade_calendar`" in call.args[0]]
        self.assertEqual(len(calendar_queries), 1)
        self.assertEqual(calendar_queries[0].args[1], ("2026-07-27",))
        asset_queries = [call for call in cursor.execute.call_args_list if "`live_asset_snapshot`" in call.args[0]]
        self.assertEqual(len(asset_queries), 3)
        for query, day in zip(asset_queries[:2], ("2026-07-27", previous_trade_date)):
            sql, params = query.args
            self.assertIn("`market_value`, `cash`, `frozen_cash`", sql)
            self.assertIn("`trader_id`, `trade_date`, `market_value`", sql)
            self.assertIn("`snapshot_type` = 'after_trading'", sql)
            self.assertIn("`trade_date` = %s", sql)
            self.assertIn("`account_id` = %s AND `trader_id` = %s", sql)
            self.assertEqual(params, [day, "account", "trader"])
        self.assertIn("`snapshot_type` = 'before_trading'", asset_queries[-1].args[0])
        self.assertIn("`write_time`", asset_queries[-1].args[0])

    def test_rejects_unreconciled_opening_and_closing_positions(self) -> None:
        with self.assertRaisesRegex(ValueError, "position reconciliation failed"):
            calculate_records(
                "2026-07-27",
                trades=[],
                before_positions=[
                    {"account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ", "stock_code": "000001.SZ", "volume": 100,
                     "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00"},
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
                    "trade_time": "2026-07-27 10:00:00",
                },
            ],
            before_positions=[
                {
                    "account_id": "a", "trader_id": "t", "instrument_id": "000001.SZ",
                    "stock_code": "000001.SZ", "volume": 100,
                    "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
                        "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
                "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
                "trade_date": "2026-07-27", "write_time": "2026-07-27 09:27:00",
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
