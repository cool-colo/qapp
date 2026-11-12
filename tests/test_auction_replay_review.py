from __future__ import annotations

import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.historical_ticks import HistoricalTick, HistoricalTickDataProvider
from backtests.data_providers.ricequant_ticks import ClickHouseRicequantTickDataProvider
from backtests.target_model_predictions.auction_simulation import AuctionReplayFillModel
from market_data import DailyStockData
from strategies.model_prediction_targets import TargetModelPredictionsStrategy
from tests.test_opening_auction import replay_fixture, timestamp


class AuctionReplayReviewTest(unittest.TestCase):
    def test_live_limits_use_nautilus_when_only_prior_day_reference_exists(self):
        engine, strategy, _, _ = replay_fixture(
            strategy_type=TargetModelPredictionsStrategy,
            transform_config=lambda config: type(config)(**{**config.dict(), "daily_stock_data": (
                DailyStockData("000001.SZ", date(2026, 7, 7), up_limit=99, down_limit=1),
            )}),
        )
        try:
            strategy.clock.set_time(timestamp("2026-07-08 09:24:50"))
            self.assertEqual(strategy._price_limits("000001.SZ.QMT"), (11, 9))
            strategy.clock.set_time(timestamp("2026-07-08 14:30:00"))
            self.assertEqual(strategy._price_limits("000001.SZ.QMT"), (11, 9))
        finally:
            engine.dispose()

    def test_tick_backtest_limits_use_the_historical_trading_day(self):
        engine, strategy, _, _ = replay_fixture(transform_config=lambda config: type(config)(
            **{**config.dict(), "daily_stock_data": (
                DailyStockData("000001.SZ", date(2026, 7, 8), up_limit=12, down_limit=8),
                DailyStockData("000001.SZ", date(2026, 7, 9), up_limit=13, down_limit=7),
            )}))
        try:
            for day, limits in [(8, (12, 8)), (9, (13, 7)), (10, (None, None))]:
                strategy.clock.set_time(timestamp(f"2026-07-{day:02d} 14:30:00"))
                self.assertEqual(strategy._price_limits("000001.SZ.QMT"), limits)
        finally:
            engine.dispose()

    def test_comparison_waits_for_open_results_received_50_ms_apart(self):
        def delay_second_result(rows):
            return [replace(row, ts_init=row.ts_init + 50_000_000)
                    if row.symbol == "000002.SZ" and row.kind == "auction" else row
                    for row in rows]

        engine, strategy, planner, _ = replay_fixture(
            auction_enabled=False, transform_rows=delay_second_result)
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual({c.stock_code for c in planner.requests[0].candidates},
                             {"000001.SZ", "000002.SZ"})
        finally:
            engine.dispose()

    def test_comparison_timer_cannot_plan_before_late_open_result(self):
        def delay_second_result(rows):
            return [replace(row, ts_init=timestamp("2026-07-08 09:30:10"))
                    if row.symbol == "000002.SZ" and row.kind == "auction" else row
                    for row in rows]

        engine, strategy, planner, _ = replay_fixture(
            auction_enabled=False, transform_rows=delay_second_result)
        planning_times = []
        original_plan = planner.plan

        def record_time(request):
            planning_times.append(strategy.clock.timestamp_ns())
            return original_plan(request)

        planner.plan = record_time
        try:
            engine.run()
            self.assertEqual(planning_times, [timestamp("2026-07-08 09:30:10")])
            self.assertEqual(len(planner.requests[0].candidates), 2)
        finally:
            engine.dispose()

    def test_comparison_missing_required_open_never_freezes_an_incomplete_plan(self):
        engine, strategy, planner, _ = replay_fixture(auction_enabled=False, transform_rows=lambda rows: [
            row for row in rows if row.symbol != "000002.SZ" or row.kind != "auction"])
        try:
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertNotIn(date(2026, 7, 8), strategy._processed_dates)
        finally:
            engine.dispose()

    def test_comparison_does_not_wait_for_known_suspended_candidate(self):
        engine, strategy, planner, _ = replay_fixture(
            auction_enabled=False,
            transform_rows=lambda rows: [row for row in rows if row.symbol != "000002.SZ"],
            transform_config=lambda config: type(config)(**{
                **config.dict(), "suspended_by_date": {"2026-07-08": ["000002.SZ"]},
            }),
        )
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual([c.stock_code for c in planner.requests[0].candidates], ["000001.SZ"])
        finally:
            engine.dispose()

    def test_delayed_continuous_quote_cannot_cross_into_closing_auction(self):
        def append_delayed_quote(rows):
            return rows + [HistoricalTick("000001.SZ", "quote",
                timestamp("2026-07-08 14:56:59"), timestamp("2026-07-08 14:57:00"),
                10, 10, 10000, 10000, 10, 10)]

        engine, strategy, _, _ = replay_fixture(transform_rows=append_delayed_quote)
        try:
            engine.run()
            self.assertEqual(engine.trader.generate_fills_report().last_qty.astype(int).sum(), 200)
        finally:
            engine.dispose()

    def test_closing_reference_quotes_cannot_fill_opening_residual_orders(self):
        def append_closing_quote(rows):
            ts = timestamp("2026-07-08 14:58:00")
            return rows + [HistoricalTick("000001.SZ", "quote", ts, ts, 10, 10,
                                         10000, 10000, 10, 10)]

        engine, strategy, _, _ = replay_fixture(transform_rows=append_closing_quote)
        try:
            engine.run()
            fills = engine.trader.generate_fills_report()
            self.assertEqual(fills.last_qty.astype(int).sum(), 200)
            self.assertFalse(any(pd.Timestamp(ts).tz_convert("Asia/Shanghai").hour == 14
                                 for ts in fills.ts_event))
        finally:
            engine.dispose()

    def test_only_explicit_closing_result_adds_bounded_closing_liquidity(self):
        def append_closing_rows(rows):
            for clock_time, kind in [("14:58:00", "closing_indicative"),
                                     ("15:00:00", "closing_auction")]:
                ts = timestamp(f"2026-07-08 {clock_time}")
                rows = rows + [HistoricalTick(code, kind, ts, ts, price + .01, price + .01,
                                             1000, 1000, price, price)
                               for code, price in [("000001.SZ", 10), ("000002.SZ", 20)]]
            return rows

        engine, strategy, _, _ = replay_fixture(transform_rows=append_closing_rows)
        try:
            engine.run()
            fills = engine.trader.generate_fills_report()
            self.assertEqual(fills.last_qty.astype(int).sum(), 220)
            closing = fills[[pd.Timestamp(ts).tz_convert("Asia/Shanghai").hour == 15
                             for ts in fills.ts_event]]
            self.assertEqual(closing.last_qty.astype(int).sum(), 20)
        finally:
            engine.dispose()

    def test_fractional_final_timestamp_does_not_extend_opening_submission_cutoff(self):
        engine, strategy, _, _ = replay_fixture(submission_latency_ms=10025, transform_rows=lambda rows: [
            replace(row, ts_event=row.ts_event + 50_000_000, ts_init=row.ts_init + 50_000_000)
            if row.kind == "auction" else row for row in rows])
        try:
            engine.run()
            self.assertFalse(engine.trader.generate_orders_report().empty)
            self.assertTrue(engine.trader.generate_fills_report().empty)
        finally:
            engine.dispose()

    def test_csv_separates_opening_and_closing_results_and_rejects_closing_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.csv"
            opening = dict(symbol="000001.SZ", kind="auction",
                           ts_event="2026-07-08T09:25:00+08:00", ts_init="2026-07-08T09:25:00+08:00",
                           bid_price=10, ask_price=10, bid_size=1000, ask_size=1000,
                           previous_close=10, open_price=10)
            closing = dict(opening, kind="closing_auction", bid_price=10.50, ask_price=10.50,
                           ts_event="2026-07-08T15:00:00+08:00", ts_init="2026-07-08T15:00:00+08:00")

            def load(rows):
                with path.open("w", newline="") as target:
                    writer = csv.DictWriter(target, fieldnames=sorted(HistoricalTickDataProvider.COLUMNS))
                    writer.writeheader()
                    writer.writerows(rows)
                return HistoricalTickDataProvider().load(
                    str(path), start="2026-07-08", end="2026-07-09", symbols={"000001.SZ"})

            self.assertEqual([row.kind for row in load([opening, closing])], ["auction", "closing_auction"])
            with self.assertRaisesRegex(ValueError, "Multiple final closing_auction"):
                load([closing, dict(closing, ts_event="2026-07-08T15:00:00.050+08:00",
                                    ts_init="2026-07-08T15:00:00.050+08:00")])
            with self.assertRaisesRegex(ValueError, "continuous session"):
                load([dict(closing, kind="quote", ts_event="2026-07-08T14:58:00+08:00",
                           ts_init="2026-07-08T14:58:00+08:00")])
            next_day = dict(opening, ts_event="2026-07-09T09:25:00+08:00",
                            ts_init="2026-07-09T09:25:00+08:00")
            self.assertEqual(len(load([opening, next_day])), 2)

    def test_csv_rejects_two_final_results_in_the_same_second(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.csv"
            rows = [dict(symbol="000001.SZ", kind="auction",
                         ts_event=f"2026-07-08T09:25:00.{millis}+08:00",
                         ts_init=f"2026-07-08T09:25:00.{millis}+08:00",
                         bid_price=10, ask_price=10, bid_size=1000, ask_size=1000,
                         previous_close=10, open_price=10) for millis in ["050", "100"]]
            with path.open("w", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=sorted(HistoricalTickDataProvider.COLUMNS))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "Multiple final.*000001.SZ"):
                HistoricalTickDataProvider().load(str(path), start="2026-07-08", end="2026-07-08",
                                                  symbols={"000001.SZ"})

    def test_fill_model_rejects_a_second_final_result_but_reobserving_one_is_safe(self):
        model = AuctionReplayFillModel(lambda: timestamp("2026-07-08 09:25:01"))
        ts = timestamp("2026-07-08 09:25:00.050")
        row = HistoricalTick("000001.SZ", "auction", ts, ts, 10, 10, 1000, 1000, 10, 10)
        model.observe("000001.SZ.QMT", row)
        model.observe("000001.SZ.QMT", row)  # Depth and QuoteTick carry the same observation.
        with self.assertRaisesRegex(ValueError, "Multiple final"):
            model.observe("000001.SZ.QMT", replace(row, ts_event=ts + 50_000_000,
                                                   ts_init=ts + 50_000_000))

    def test_ricequant_rejects_fractional_second_duplicate_final_results(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig())
        rows = [dict(order_book_id="000001.XSHE", datetime=f"2026-07-08 09:25:00.{millis}",
                     open=10, volume=1000, prev_close=10, a1=10, b1=10,
                     a1_v=1000, b1_v=1000) for millis in ["050", "100"]]
        with self.assertRaisesRegex(ValueError, "Multiple final"):
            provider.build_ticks(rows, {"000001.XSHE": "000001.SZ"})

    def test_ricequant_closing_ticks_remain_indicative_even_at_15_00(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig())
        rows = [dict(order_book_id="000001.XSHE", datetime=f"2026-07-08 {clock_time}",
                     open=10, volume=1000000, prev_close=10, a1=10.01, b1=10,
                     a1_v=1000, b1_v=1000)
                for clock_time in ["14:56:59", "14:57:00", "14:58:00", "15:00:00"]]
        ticks = provider.build_ticks(rows, {"000001.XSHE": "000001.SZ"})
        self.assertEqual([row.kind for row in ticks],
                         ["quote", "closing_indicative", "closing_indicative", "closing_indicative"])


if __name__ == "__main__":
    unittest.main()
