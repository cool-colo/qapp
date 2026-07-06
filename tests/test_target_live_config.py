from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from lives.live_qmt_target_model_predictions import parse_args
from lives.live_qmt_target_model_predictions import normalize_refresh_time
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig


class TargetLiveConfigTest(unittest.TestCase):
    def test_model_target_default_keeps_original_position_cap(self) -> None:
        config = TargetModelPredictionsStrategyConfig(
            instrument_ids=[],
            bar_types={},
            instrument_stock_codes={},
            signals_by_date={},
            trading_dates=[],
            listed_dates={},
            st_by_date={},
            suspended_by_date={},
        )
        self.assertEqual(config.max_position_percent, 0.03)

    def test_normalize_refresh_time_accepts_hh_mm_and_hh_mm_ss(self) -> None:
        self.assertEqual(normalize_refresh_time("9:00"), "09:00")
        self.assertEqual(normalize_refresh_time("09:00:30"), "09:00")

    def test_normalize_refresh_time_rejects_malformed_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_refresh_time("9")
        with self.assertRaises(ValueError):
            normalize_refresh_time("25:00")

    def test_pre_open_reconcile_time_defaults_to_0915(self) -> None:
        with patch.dict(
            os.environ,
            {"QMT_ACCOUNT_ID": "TEST", "MODEL_LIVE_PRE_OPEN_RECONCILE_TIME": "09:15"},
            clear=False,
        ):
            with patch.object(sys, "argv", ["test"]):
                args = parse_args()
        self.assertEqual(args.pre_open_reconcile_time, "09:15")

    def test_pre_open_reconcile_time_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"QMT_ACCOUNT_ID": "TEST"}, clear=False):
            with patch.object(sys, "argv", ["test", "--pre-open-reconcile-time", ""]):
                args = parse_args()
        self.assertIsNone(args.pre_open_reconcile_time)


if __name__ == "__main__":
    unittest.main()
