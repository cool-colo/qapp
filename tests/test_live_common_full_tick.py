from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

from lives.live_common import LivePredictionDataLoader


def _loader() -> LivePredictionDataLoader:
    # Build without touching ClickHouse: only the full-tick HTTP path is exercised.
    loader = LivePredictionDataLoader.__new__(LivePredictionDataLoader)
    loader.args = argparse.Namespace(
        base_url_http="http://proxy.local",
        api_key="key",
        clickhouse_timeout_secs=5.0,
    )
    return loader


class FullTickSnapshotTest(unittest.TestCase):
    def test_full_tick_snapshot_keys_by_instrument_id_and_coerces_fields(self) -> None:
        loader = _loader()
        payload = [
            {"symbol": "000001.SZ", "tick": {"open": "53.09", "last_price": "54.0", "volume": "100"}},
            {"symbol": "000002.SZ", "tick": {"open": 20.0}},
            {"symbol": "999999.SZ", "tick": {"open": 1.0}},  # not requested -> dropped
        ]
        with patch.object(loader, "_post_full_tick", return_value=payload) as mock_post:
            snapshot = loader.full_tick_snapshot(["000001.SZ", "000002.SZ"])

        mock_post.assert_called_once()
        self.assertEqual(snapshot["000001.SZ.QMT"]["open"], 53.09)
        self.assertEqual(snapshot["000001.SZ.QMT"]["last_price"], 54.0)
        self.assertEqual(snapshot["000002.SZ.QMT"]["open"], 20.0)
        self.assertNotIn("999999.SZ.QMT", snapshot)

    def test_full_tick_snapshot_empty_inputs(self) -> None:
        loader = _loader()
        self.assertEqual(loader.full_tick_snapshot([]), {})

    def test_full_tick_snapshot_requires_base_url(self) -> None:
        loader = _loader()
        loader.args.base_url_http = ""
        self.assertEqual(loader.full_tick_snapshot(["000001.SZ"]), {})


if __name__ == "__main__":
    unittest.main()
