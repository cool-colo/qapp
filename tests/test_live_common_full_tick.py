from __future__ import annotations

import argparse
import asyncio
import urllib.error
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from lives.broker_data_source import QmtBrokerDataSource
from lives.live_common import LivePredictionDataLoader


def _loader() -> LivePredictionDataLoader:
    # Provider construction does not query ClickHouse; broker I/O is mocked below.
    args = argparse.Namespace(
        base_url_http="http://proxy.local",
        api_key="key",
        account_id="ACC",
        account_type="STOCK",
        clickhouse_timeout_secs=5.0,
    )
    return LivePredictionDataLoader(
        args,
        ClickHouseConnectionConfig(),
        broker_source=QmtBrokerDataSource(args),
    )


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FullTickSnapshotTest(unittest.TestCase):
    def test_full_tick_snapshot_keys_by_instrument_id_and_coerces_fields(self) -> None:
        loader = _loader()
        payload = [
            {"symbol": "000001.SZ", "tick": {"open": "53.09", "last_price": "54.0", "volume": "100"}},
            {"symbol": "000002.SZ", "tick": {"open": 20.0}},
            {"symbol": "999999.SZ", "tick": {"open": 1.0}},  # not requested -> dropped
        ]
        with (
            patch.object(loader._broker_source, "_post_full_tick", return_value=payload) as mock_post,
            self.assertLogs("lives.broker_data_source", level="WARNING") as logs,
        ):
            snapshot = loader.full_tick_snapshot(["000001.SZ", "000002.SZ"])

        mock_post.assert_called_once_with("http://proxy.local", "key", ["000001.SZ", "000002.SZ"])
        self.assertEqual(snapshot["000001.SZ.QMT"]["open"], 53.09)
        self.assertEqual(snapshot["000001.SZ.QMT"]["last_price"], 54.0)
        self.assertEqual(snapshot["000002.SZ.QMT"]["open"], 20.0)
        self.assertNotIn("999999.SZ.QMT", snapshot)
        self.assertEqual(
            logs.output,
            ["WARNING:lives.broker_data_source:full-tick snapshot returned unrequested symbol: 999999.SZ"],
        )

    def test_full_tick_snapshot_empty_inputs(self) -> None:
        loader = _loader()
        with self.assertLogs("lives.broker_data_source", level="ERROR") as logs:
            self.assertEqual(loader.full_tick_snapshot([]), {})
        self.assertIn("empty stock_codes", logs.output[0])

    def test_full_tick_snapshot_requires_base_url(self) -> None:
        loader = _loader()
        loader.args.base_url_http = ""
        with self.assertLogs("lives.broker_data_source", level="ERROR") as logs:
            self.assertEqual(loader.full_tick_snapshot(["000001.SZ"]), {})
        self.assertIn("base_url_http", logs.output[0])

    def test_full_tick_snapshot_logs_unmatched_symbol_and_unusable_tick(self) -> None:
        loader = _loader()
        payload = [
            {"symbol": "999999.SZ", "tick": {"open": 1.0}},
            {"symbol": "000001.SZ", "tick": {"open": None}},
        ]

        with (
            patch.object(loader._broker_source, "_post_full_tick", return_value=payload),
            self.assertLogs("lives.broker_data_source", level="WARNING") as logs,
        ):
            self.assertEqual(loader.full_tick_snapshot(["000001.SZ"]), {})

        self.assertIn("WARNING:lives.broker_data_source:full-tick snapshot returned unrequested symbol: 999999.SZ", logs.output)
        self.assertIn(
            "ERROR:lives.broker_data_source:full-tick snapshot returned unusable tick for 000001.SZ: {'open': None}",
            logs.output,
        )

    def test_post_full_tick_retries_failed_requests(self) -> None:
        loader = _loader()
        attempts = []

        def fake_urlopen(request, timeout):
            attempts.append((request, timeout))
            if len(attempts) < 3:
                raise urllib.error.URLError("temporary failure")
            return _Response(
                b'{"success": true, "data": {"items": [{"symbol": "000001.SZ", "tick": {"open": 1}}]}}',
            )

        with (
            patch("lives.broker_data_source.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("lives.broker_data_source.time.sleep") as sleep,
            self.assertLogs("lives.broker_data_source", level="WARNING") as logs,
        ):
            rows = loader._broker_source._post_full_tick("http://proxy.local", "key", ["000001.SZ"])

        self.assertEqual(rows, [{"symbol": "000001.SZ", "tick": {"open": 1}}])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1])
        self.assertEqual(len(logs.records), 2)

    def test_post_full_tick_raises_after_five_failures(self) -> None:
        loader = _loader()

        with (
            patch(
                "lives.broker_data_source.urllib.request.urlopen",
                side_effect=urllib.error.URLError("proxy unavailable"),
            ) as urlopen,
            patch("lives.broker_data_source.time.sleep") as sleep,
            self.assertLogs("lives.broker_data_source", level="WARNING") as logs,
        ):
            with self.assertRaisesRegex(RuntimeError, "after 5 attempts"):
                loader._broker_source._post_full_tick("http://proxy.local", "key", ["000001.SZ"])

        self.assertEqual(urlopen.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(len(logs.records), 4)

    def test_broker_position_snapshot_keys_by_instrument_id(self) -> None:
        loader = _loader()
        rows = [
            {
                "stock_code": "000720",
                "volume": "143100",
                "can_use_volume": "107400",
                "avg_price": "3.40",
                "last_price": "3.26",
                "market_value": "466506.00",
            },
        ]
        with patch.object(loader._broker_source, "_with_broker_session", new=AsyncMock(return_value=rows)):
            snapshot = asyncio.run(loader.broker_position_snapshot())

        position = snapshot["000720.SZ.QMT"]
        self.assertEqual(position["stock_code"], "000720.SZ")
        self.assertEqual(position["volume"], "143100")
        self.assertEqual(position["market_value"], "466506.00")
        self.assertEqual(position["raw"]["last_price"], "3.26")


if __name__ == "__main__":
    unittest.main()
