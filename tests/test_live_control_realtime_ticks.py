from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from lives.control_server import LiveControlServer
from strategies.target_quantities import TickSnapshot


class FakeStrategy:
    def __init__(self) -> None:
        self.config = SimpleNamespace(full_tick_refresh_secs=1.0)
        self._instruments = {"000001.SZ": "000001.SZ.BIGQMT"}
        self._ticks = {
            "000001.SZ.BIGQMT": TickSnapshot(
                open=10.0,
                last_price=10.5,
                last_close=9.9,
                high=10.8,
                low=9.8,
                amount=123456.0,
                volume=12000,
            ),
        }

    def instrument_id_for_stock_code(self, stock_code):
        return self._instruments.get(stock_code)

    def tick_snapshot_for(self, instrument_id):
        return self._ticks.get(instrument_id)

    def market_snapshot_updated_at_ns(self):
        return 1_735_776_000_000_000_000

    def control_clock_date(self):
        return date(2025, 1, 2)


class RealtimeTicksPayloadTest(unittest.TestCase):
    def test_returns_only_requested_available_ticks_and_reports_missing(self) -> None:
        server = LiveControlServer(
            node=SimpleNamespace(),
            strategy_ref=FakeStrategy(),
            account_id="A1",
            trader_id="T1",
        )

        result = server._realtime_ticks_payload(
            ["000001.SZ", "600000.SH", "000001.SZ"],
        )

        self.assertEqual(result["trade_date"], "2025-01-02")
        self.assertEqual(result["refresh_interval_secs"], 1.0)
        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["missing_stock_codes"], ["600000.SH"])
        self.assertEqual(
            result["ticks"],
            [
                {
                    "stock_code": "000001.SZ",
                    "open": 10.0,
                    "last_price": 10.5,
                    "last_close": 9.9,
                    "high": 10.8,
                    "low": 9.8,
                    "amount": 123456.0,
                    "volume": 12000,
                    "price_source": "market_status",
                },
            ],
        )
        self.assertIsNotNone(result["snapshot_time"])
        self.assertIsNotNone(result["server_time"])


if __name__ == "__main__":
    unittest.main()
