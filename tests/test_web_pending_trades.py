from __future__ import annotations

import unittest

from web.app import build_pending_trade_projection
from web.app import realtime_pending_trades


class FakeNodeApi:
    def __init__(self, targets, ticks) -> None:
        self.targets = targets
        self.ticks = ticks
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/strategy/targets":
            return self.targets
        if path == "/realtime/ticks":
            return self.ticks
        raise AssertionError(path)


class FakeData:
    def __init__(self, node) -> None:
        self.node = node

    def node_api(self, account, trader):
        assert (account, trader) == ("A1", "T1")
        return self.node

    def attach_names(self, rows):
        return [{**row, "stock_name": row.get("stock_name") or "测试股票"} for row in rows]


class PendingTradeProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = {
            "target_date": "2026-11-12",
            "target_version": "target-v1",
            "targets": [
                {
                    "stock_code": "000001.SZ",
                    "name": "买入股票",
                    "current_qty": 0,
                    "target_qty": 100,
                    "frozen": False,
                },
                {
                    "stock_code": "600000.SH",
                    "name": "卖出股票",
                    "current_qty": 200,
                    "target_qty": 100,
                    "frozen": False,
                },
                {
                    "stock_code": "300001.SZ",
                    "name": "已达标",
                    "current_qty": 100,
                    "target_qty": 100,
                    "frozen": False,
                },
            ],
        }
        self.ticks = {
            "trade_date": "2026-11-12",
            "snapshot_time": "2026-11-12T02:00:00+00:00",
            "refresh_interval_secs": 1.0,
            "missing_stock_codes": [],
            "ticks": [
                {"stock_code": "000001.SZ", "open": 10, "last_price": 11},
                {"stock_code": "600000.SH", "open": 20, "last_price": 18},
            ],
        }

    def test_values_only_unfinished_targets_and_side_aware_loss(self) -> None:
        result = build_pending_trade_projection(self.targets, self.ticks)

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["unpriced_count"], 0)
        self.assertEqual([row["side"] for row in result["trades"]], ["BUY", "SELL"])
        self.assertEqual(result["trades"][0]["loss_amount"], 100.0)
        self.assertEqual(result["trades"][1]["loss_amount"], 200.0)
        self.assertEqual(result["buy"]["open_amount"], 1000.0)
        self.assertEqual(result["buy"]["current_amount"], 1100.0)
        self.assertEqual(result["sell"]["open_amount"], 2000.0)
        self.assertEqual(result["sell"]["current_amount"], 1800.0)
        self.assertEqual(result["total"]["loss_amount"], 300.0)
        self.assertAlmostEqual(result["total"]["loss_rate_open"], 300 / 3000)
        self.assertAlmostEqual(result["total"]["loss_rate_current"], 300 / 2900)
        self.assertEqual(result["total"]["open_net_cash"], -1000.0)
        self.assertEqual(result["total"]["current_net_cash"], -700.0)

    def test_keeps_unpriced_trade_visible_but_excludes_it_from_totals(self) -> None:
        self.targets["targets"].append(
            {
                "stock_code": "300002.SZ",
                "name": "无行情",
                "current_qty": 0,
                "target_qty": 100,
                "frozen": True,
                "frozen_reason": "停牌",
            }
        )

        result = build_pending_trade_projection(self.targets, self.ticks)

        self.assertEqual(result["count"], 3)
        self.assertEqual(result["unpriced_count"], 1)
        self.assertIsNone(result["trades"][-1]["loss_amount"])
        self.assertEqual(result["total"]["loss_amount"], 300.0)

    def test_endpoint_reads_both_live_node_views_and_attaches_names(self) -> None:
        node = FakeNodeApi(self.targets, self.ticks)

        result = realtime_pending_trades("A1", "T1", FakeData(node))

        self.assertEqual(
            node.calls,
            [
                ("/strategy/targets", None),
                (
                    "/realtime/ticks",
                    {"stock_codes": "000001.SZ,600000.SH"},
                ),
            ],
        )
        self.assertEqual(result["target_version"], "target-v1")
        self.assertEqual(result["trades"][0]["name"], "买入股票")
        self.assertEqual(result["snapshot_time"], "2026-11-12T02:00:00+00:00")
        self.assertEqual(result["refresh_interval_secs"], 1.0)


if __name__ == "__main__":
    unittest.main()
