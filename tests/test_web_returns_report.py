from __future__ import annotations

import unittest
from decimal import Decimal

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
