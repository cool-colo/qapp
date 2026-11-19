from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

from web.returns_report import RETURNS_SQL, build_return_summary
from web.db import jsonable_rows


class WebReturnsReportTests(unittest.TestCase):
    def test_strategy_daily_rate_uses_previous_after_trading_net_value(self) -> None:
        self.assertIn(
            "r.return_amount / NULLIF(an.previous_net_value, 0)",
            RETURNS_SQL,
        )
        self.assertIn("market_value + cash + frozen_cash", RETURNS_SQL)
        self.assertNotIn("after_market_value - r.return_amount", RETURNS_SQL)

    def test_reports_exclude_deposit_before_computing_net_value_endpoints(self) -> None:
        canonical_sql = (
            Path(__file__).resolve().parents[1] / "scripts/weekly_slippage_return_report.sql"
        ).read_text()
        # Execute the actual asset CTEs in memory; these SQL operations are shared
        # by SQLite and MySQL, unlike the report's later risk/formatting functions.
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE live_asset_snapshot (account_id TEXT, trader_id TEXT, trade_date TEXT, "
                "snapshot_type TEXT, market_value NUMERIC, cash NUMERIC, frozen_cash NUMERIC)",
            )
            connection.executemany(
                "INSERT INTO live_asset_snapshot VALUES (?, ?, ?, 'after_trading', 0, ?, 0)",
                [
                    (account_id, "trader", day, cash)
                    for account_id in ("8886569186", "other")
                    for day, cash in (
                        ("2026-11-17", 990000), ("2026-11-18", 1000000),
                        ("2026-11-19", 1160000), ("2026-11-20", 1165000),
                    )
                ],
            )
            for source_sql in (RETURNS_SQL, canonical_sql):
                start = source_sql.index("asset_nav_history AS (")
                end = source_sql.index("\n),", source_sql.index("asset_nav AS (")) + 2
                ctes = source_sql[start:end]
                ctes = re.sub(r"%\((\w+)\)s", r":\1", ctes)
                ctes = re.sub(r"@(\w+)", r":\1", ctes)
                sql = (
                    "WITH " + ctes + " SELECT * FROM asset_nav "
                    "WHERE trade_date BETWEEN :start_date AND :end_date ORDER BY trade_date"
                )
                for account_id in ("8886569186", "other"):
                    with self.subTest(web=source_sql == RETURNS_SQL, account_id=account_id):
                        params = {
                            "account_id": account_id, "trader_id": "trader",
                            "start_date": "2026-11-18", "end_date": "2026-11-20",
                        }
                        rows = [dict(row) for row in connection.execute(sql, params)]
                        expected = (
                            [(1000000, 990000), (1010000, 1000000), (1015000, 1010000)]
                            if account_id == "8886569186" else
                            [(1000000, 990000), (1160000, 1000000), (1165000, 1160000)]
                        )
                        self.assertEqual(
                            [(row["net_value"], row["previous_net_value"]) for row in rows], expected,
                        )
                        summary = build_return_summary(rows[1:])
                        self.assertIsNotNone(summary)
                        assert summary is not None
                        self.assertEqual(summary["return_amount"], 15000 if account_id == "8886569186" else 165000)
                        self.assertAlmostEqual(summary["strat_daily_rate"], 0.015 if account_id == "8886569186" else 0.165)

                        # A range starting after the deposit must also adjust its
                        # previous-day lookback, not just the displayed dates.
                        params["start_date"] = "2026-11-20"
                        rows = [dict(row) for row in connection.execute(sql, params)]
                        self.assertEqual(len(rows), 1)
                        self.assertEqual((rows[0]["net_value"], rows[0]["previous_net_value"]), expected[-1])

    def test_weekly_and_period_summary_use_net_value_endpoints(self) -> None:
        self.assertIn(
            "net_value - FIRST_VALUE(previous_net_value) OVER w",
            RETURNS_SQL,
        )

        rows = [
            {
                "net_value": Decimal("110"),
                "previous_net_value": Decimal("100"),
                "csi1000_close": Decimal("110"),
                "csi1000_preclose": Decimal("100"),
            },
            {
                "net_value": Decimal("121"),
                "previous_net_value": Decimal("110"),
                "csi1000_close": Decimal("121"),
                "csi1000_preclose": Decimal("110"),
            },
        ]
        summary = build_return_summary(rows)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(Decimal("21"), summary["return_amount"])
        self.assertEqual(Decimal("0.21"), summary["strat_daily_rate"])
        self.assertEqual(Decimal("0.21"), summary["csi1000_daily_rate"])
        self.assertEqual(Decimal("0"), summary["excess_daily_rate"])
        self.assertEqual(Decimal("21"), summary["week_cum_return_amount"])

    def test_summary_values_are_json_numbers(self) -> None:
        summary = build_return_summary(
            [
                {
                    "net_value": Decimal("110"),
                    "previous_net_value": Decimal("100"),
                    "csi1000_close": Decimal("110"),
                    "csi1000_preclose": Decimal("100"),
                }
            ]
        )

        self.assertIsNotNone(summary)
        assert summary is not None
        serialized = jsonable_rows([summary])[0]
        self.assertEqual(0.1, serialized["strat_daily_rate"])
        self.assertEqual(10.0, serialized["return_amount"])


if __name__ == "__main__":
    unittest.main()
