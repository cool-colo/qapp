from __future__ import annotations

import unittest

from strategies.strategy_params import StrategyParams


class StrategyParamsPriceTrendTest(unittest.TestCase):
    def test_repository_config_loads_price_trend_defaults(self) -> None:
        params = StrategyParams.from_yaml("configs/strategy.yaml")

        self.assertEqual(params.unfilled_timeout_secs, 3.0)
        self.assertEqual(params.resubmit_check_interval_secs, 1.0)
        self.assertEqual(params.process_targets_interval_secs, 2.0)
        self.assertEqual(params.buy_offset_bps, 50.0)
        self.assertEqual(params.sell_offset_bps, 50.0)
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
