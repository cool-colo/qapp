from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
import tempfile

from strategies.strategy_params import StrategyParams


class StrategyParamsPriceTrendTest(unittest.TestCase):
    def test_repository_config_loads_price_trend_and_execution_settings(self) -> None:
        params = StrategyParams.from_yaml("configs/strategy.yaml")

        self.assertEqual(params.resubmit_check_interval_secs, 0.5)
        self.assertEqual(params.process_targets_interval_secs, 2.0)
        self.assertEqual(params.execution_windows, "14:30-14:56")
        self.assertEqual(params.cancel_windows, "09:35-11:30,13:00-14:56")
        self.assertEqual(params.exchange_cancel_windows, "09:35-11:30,13:00-14:56")
        self.assertEqual(params.submission_book_max_age_secs, 5.0)
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


class StrategyParamsBuyMinimumTest(unittest.TestCase):
    def test_default_auction_window_matches_all_strategy_configs(self):
        from strategies.target_quantities import TargetQuantityStrategyConfig

        for config in (StrategyParams(), TargetQuantityStrategyConfig(instrument_ids=[], bar_types={})):
            self.assertEqual(config.auction_execution_time, "09:24:58")
            self.assertEqual(config.auction_submit_cutoff, "09:25:00")

    def test_all_repository_configs_load_new_minimums(self):
        for path in Path("configs").glob("strategy*.yaml"):
            with self.subTest(path=path):
                params = StrategyParams.from_yaml(str(path))
                self.assertEqual(params.auction_min_buy_notional, Decimal(10000))
                self.assertEqual(params.continuous_buy_min_notional, Decimal(10000))
                self.assertEqual(params.auction_execution_time, "09:24:58")
                self.assertEqual(params.auction_submit_cutoff, "09:25:00")
                self.assertEqual(params.auction_buy_offset_bps, 80.0)
                self.assertEqual(params.auction_sell_offset_bps, 50.0)

    def test_yaml_minimums_are_decimal_and_zero_disables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.yaml"
            path.write_text('auction_min_buy_notional: "12345.67"\ncontinuous_buy_min_notional: 0\n')
            params = StrategyParams.from_yaml(str(path))
        self.assertEqual(params.auction_min_buy_notional, Decimal("12345.67"))
        self.assertIsInstance(params.auction_min_buy_notional, Decimal)
        self.assertEqual(params.continuous_buy_min_notional, Decimal(0))

    def test_invalid_minimums_fail_fast(self):
        for name in ("auction_min_buy_notional", "continuous_buy_min_notional"):
            for value in (Decimal(-1), Decimal("NaN"), Decimal("Infinity")):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    StrategyParams(**{name: value})

    def test_removed_auction_threshold_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.yaml"
            path.write_text('auction_partial_min_notional: 58000\n')
            with self.assertRaisesRegex(ValueError, "Unknown strategy config"):
                StrategyParams.from_yaml(str(path))

if __name__ == "__main__":
    unittest.main()
