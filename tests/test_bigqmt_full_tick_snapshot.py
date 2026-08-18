from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from scripts._full_tick_snapshot_common import SHANGHAI_TZ
from scripts._full_tick_snapshot_common import build_rows
from scripts.bigqmt_full_tick_snapshot_to_clickhouse import fetch_full_tick
from scripts.bigqmt_full_tick_snapshot_to_clickhouse import load_universe
from scripts.bigqmt_full_tick_snapshot_to_clickhouse import normalize_tick_payload


def _args(**overrides):
    values = {
        "sector": "沪深A股",
        "include_beijing": True,
        "max_symbols": 0,
        "max_attempts": 1,
        "chunk_size": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class BigQmtFullTickSnapshotTest(TestCase):
    def test_load_universe_combines_sector_and_beijing_equities(self) -> None:
        xtdata = MagicMock()
        xtdata.get_stock_list_in_sector.return_value = [
            "000001.SZ",
            "600000.SH",
            "000001.SZ",
        ]
        xtdata.get_full_tick.return_value = {
            "430047.BJ": {},
            "920001.BJ": {},
            "000001.SZ": {},
            "899001.SH": {},
        }

        universe = load_universe(_args(), xtdata)

        self.assertEqual(
            universe.symbols,
            ["000001.SZ", "600000.SH", "430047.BJ", "920001.BJ"],
        )
        self.assertEqual(set(universe.beijing_ticks), {"430047.BJ", "920001.BJ"})
        xtdata.get_stock_list_in_sector.assert_called_once_with("沪深A股")
        xtdata.get_full_tick.assert_called_once_with(["BJ"])

    def test_load_universe_skips_beijing_when_sector_already_fills_cap(self) -> None:
        xtdata = MagicMock()
        xtdata.get_stock_list_in_sector.return_value = ["000001.SZ", "600000.SH"]

        universe = load_universe(
            _args(max_symbols=1),
            xtdata,
        )

        self.assertEqual(universe.symbols, ["000001.SZ"])
        self.assertEqual(universe.beijing_ticks, {})
        xtdata.get_full_tick.assert_not_called()

    def test_normalize_tick_payload_preserves_depth_and_scalar_fields(self) -> None:
        tick = normalize_tick_payload(
            {
                "time": 1_725_000_000_000,
                "lastPrice": 10.2,
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "lastClose": 9.8,
                "amount": 1234.5,
                "volume": 1000,
                "pvolume": 2000,
                "openInt": 13,
                "stockStatus": 1,
                "lastSettlementPrice": 9.7,
                "transactionNum": 42,
                "askPrice": [10.21, 10.22],
                "bidPrice": [10.19, 10.18],
                "askVol": [100, 200],
                "bidVol": [300, 400],
            },
        )

        self.assertEqual(tick["time_ms"], 1_725_000_000_000)
        self.assertEqual(tick["last_price"], 10.2)
        self.assertEqual(tick["open_int"], 13)
        self.assertEqual(tick["stock_status"], 1)
        self.assertEqual(tick["transaction_num"], 42)
        self.assertEqual(tick["ask_price"], [10.21, 10.22])
        self.assertEqual(tick["bid_vol"], [300, 400])

    def test_fetch_full_tick_chunks_and_builds_existing_ods_shape(self) -> None:
        xtdata = MagicMock()
        xtdata.get_full_tick.return_value = {
            "000001.SZ": {
                "time": 1_725_000_000_000,
                "lastPrice": 10.2,
                "askPrice": [10.21],
                "bidPrice": [10.19],
                "askVol": [100],
                "bidVol": [200],
            },
            "UNREQUESTED.SZ": {"lastPrice": 1.0},
        }

        items = fetch_full_tick(
            _args(),
            xtdata,
            ["000001.SZ", "600000.SH", "920238.BJ"],
            {"920238.BJ": {"lastPrice": 5.5}},
        )
        rows = build_rows(
            items,
            datetime(2026, 8, 18, 10, 0, tzinfo=SHANGHAI_TZ),
        )

        xtdata.get_full_tick.assert_called_once_with(["000001.SZ", "600000.SH"])
        self.assertEqual([row["symbol"] for row in rows], ["000001.SZ", "920238.BJ"])
        self.assertEqual(rows[0]["trade_date"], "2026-08-18")
        self.assertEqual(rows[0]["ask_price"], [10.21])
        self.assertEqual(rows[0]["bid_vol"], [200])
        self.assertIn("last_settlement_price", rows[0])
