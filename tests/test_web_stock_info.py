from __future__ import annotations

import unittest
from unittest.mock import patch

from web.config import ClickHouseConfig
from web.db import ClickHouseClient


class StockInfoQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ClickHouseClient(ClickHouseConfig())

    def test_uses_dwd_classifications_available_as_of_requested_date(self) -> None:
        sql = self.client.stock_info_query("000001.SZ", "2026-10-30")

        self.assertIn("FROM dwd_sw_industry_member FINAL", sql)
        self.assertIn("FROM dwd_dc_concept_cons", sql)
        self.assertIn("LEFT JOIN dwd_dc_concept", sql)
        self.assertIn("FROM dwd_dc_member", sql)
        self.assertIn("LEFT JOIN dwd_dc_index", sql)
        self.assertIn("available_date <= toDate('2026-10-30')", sql)
        self.assertEqual(sql.count("available_trade_date <= toDate('2026-10-30')"), 2)
        self.assertIn("AS sw_industry_l1", sql)
        self.assertIn("AS sw_industry_l2", sql)
        self.assertIn("AS sw_industry_l3", sql)
        self.assertIn("AS dc_concepts", sql)
        self.assertIn("AS dc_boards", sql)

    def test_quotes_stock_code_and_date_literals(self) -> None:
        sql = self.client.stock_info_query("000001.SZ' OR 1=1", "2026-10-30' OR 1=1")

        self.assertIn("'000001.SZ\\' OR 1=1'", sql)
        self.assertIn("toDate('2026-10-30\\' OR 1=1')", sql)

    def test_bulk_classifications_use_dwd_as_of_data_and_normalize_arrays(self) -> None:
        rows = [{
            "stock_code": "000001.SZ",
            "sw_industry_l1": "银行",
            "sw_industry_l2": "股份制银行Ⅱ",
            "sw_industry_l3": "股份制银行Ⅲ",
            "dc_concepts": ["跨境支付"],
            "dc_member_concepts": None,
            "dc_member_industries": ["银行"],
            "dc_member_regions": ["广东板块"],
        }]
        with patch.object(self.client, "query", return_value=rows) as query:
            result = self.client.stock_classifications(
                ["000001.SZ", "000001.SZ"],
                "2026-10-30",
            )
            cached = self.client.stock_classifications(["000001.SZ"], "2026-10-30")

        sql = query.call_args.args[0]
        self.assertEqual(query.call_count, 1)
        self.assertIn("FROM dwd_sw_industry_member FINAL", sql)
        self.assertIn("FROM dwd_dc_concept_cons", sql)
        self.assertIn("FROM dwd_dc_member", sql)
        self.assertIn("board.name", sql)
        self.assertIn("board.idx_type", sql)
        self.assertIn("ifNull(board.name, '') != '融资融券'", sql)
        self.assertIn("= '概念板块'", sql)
        self.assertIn("= '行业板块'", sql)
        self.assertIn("= '地域板块'", sql)
        self.assertIn("available_date <= toDate('2026-10-30')", sql)
        self.assertEqual(sql.count("available_trade_date <= toDate('2026-10-30')"), 2)
        self.assertEqual(result["000001.SZ"]["dc_member_concepts"], [])
        self.assertEqual(result["000001.SZ"]["dc_member_industries"], ["银行"])
        self.assertEqual(result["000001.SZ"]["dc_member_regions"], ["广东板块"])
        self.assertIs(cached, result)

    def test_bulk_classifications_skip_query_for_no_codes(self) -> None:
        with patch.object(self.client, "query") as query:
            result = self.client.stock_classifications([], "2026-10-30")

        self.assertEqual(result, {})
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
