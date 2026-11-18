from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock
from unittest.mock import patch

from lives.broker_data_source import BigQmtBrokerDataSource
from lives.broker_data_source import QmtBrokerDataSource
from lives.broker_data_source import coerce_tick_fields


class BrokerTickTimestampTest(TestCase):
    def test_bigqmt_snapshot_keeps_each_symbols_exchange_timestamp(self) -> None:
        source = BigQmtBrokerDataSource(SimpleNamespace(account_id="test-account"))
        source._xtdata = MagicMock()
        source._xtdata.get_full_tick.return_value = {
            "000001.SZ": {"time": 1_794_965_092_123, "lastPrice": 10.0},
            "000002.SZ": {"time": 1_794_965_091_987, "lastPrice": 20.0},
        }
        with patch.object(source, "_ensure_client"):
            snapshot = source.full_tick_snapshot(["000001.SZ", "000002.SZ"])

        self.assertEqual(snapshot["000001.SZ.BIGQMT"]["ts_event"], 1_794_965_092_123_000_000)
        self.assertEqual(snapshot["000002.SZ.BIGQMT"]["ts_event"], 1_794_965_091_987_000_000)
        self.assertIsInstance(snapshot["000001.SZ.BIGQMT"]["ts_event"], int)

    def test_qmt_snapshot_uses_source_milliseconds(self) -> None:
        source = QmtBrokerDataSource(SimpleNamespace(base_url_http="http://proxy.local", api_key=None))
        with patch.object(source, "_post_full_tick", return_value=[
            {"symbol": "000001.SZ", "tick": {"time_ms": "1794965092123", "last_price": 10}},
        ]):
            snapshot = source.full_tick_snapshot(["000001.SZ"])

        self.assertEqual(snapshot["000001.SZ.QMT"]["ts_event"], 1_794_965_092_123_000_000)

    def test_nanosecond_timestamp_does_not_pass_through_float(self) -> None:
        timestamp = 1_794_965_092_123_456_789
        self.assertEqual(coerce_tick_fields({"ts_event": timestamp})["ts_event"], timestamp)

    def test_missing_exchange_timestamp_stays_unavailable(self) -> None:
        for raw_tick in ({"lastPrice": 10}, {"lastPrice": 10, "time": None},
                         {"lastPrice": 10, "time": 0}):
            with self.subTest(raw_tick=raw_tick):
                self.assertNotIn("ts_event", BigQmtBrokerDataSource._normalize_tick(raw_tick))
