from __future__ import annotations

import unittest

from strategies.strategy_params import StrategyParams


class StrategyParamsPriceTrendTest(unittest.TestCase):
    def test_repository_config_loads_price_trend_defaults(self) -> None:
        params = StrategyParams.from_yaml("configs/strategy.yaml")

        self.assertEqual(params.resubmit_check_interval_secs, 0.5)
        self.assertEqual(params.process_targets_interval_secs, 2.0)
        self.assertEqual(params.execution_windows, "14:30-14:55")
        self.assertEqual(params.cancel_windows, "09:30-11:30,13:00-14:57")
        self.assertEqual(params.exchange_cancel_windows, "09:30-11:30,13:00-14:57")
        self.assertEqual(params.submission_book_max_age_secs, 4.0)
        self.assertEqual(params.cancel_book_max_age_secs, 4.0)
        self.assertEqual(params.same_side_price_improvement_threshold, 20)
        self.assertEqual(params.price_mismatch_cancel_delay_secs, 30.0)
        self.assertFalse(params.price_trend_effect_enabled)
        self.assertEqual(params.price_trend_window_secs, 10.0)
        self.assertEqual(params.price_trend_threshold_bps, 20.0)
        self.assertEqual(params.price_trend_max_bps, 40.0)
        self.assertEqual(params.price_trend_confirmation_strength, 0.5)
        self.assertEqual(params.price_trend_book_max_age_secs, 2.0)
        self.assertEqual(params.price_trend_min_timeout_multiplier, 0.3)
        self.assertEqual(params.price_trend_max_offset_multiplier, 3.0)

    def test_invalid_price_trend_range_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "price_trend_max_bps"):
            StrategyParams(
                price_trend_threshold_bps=40.0,
                price_trend_max_bps=20.0,
            )

if __name__ == "__main__":
    unittest.main()
