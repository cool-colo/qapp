"""Real Nautilus replay of cached-price execution and auction boundaries."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from msgspec.structs import replace as config_replace

from backtests.target_model_predictions.run_backtest import BacktestTargetModelPredictionsStrategy
from tests.test_opening_auction import replay_fixture, timestamp

A, B = "000001.SZ.QMT", "000002.SZ.QMT"


class CachedExecutionPriceStrategy(BacktestTargetModelPredictionsStrategy):
    def _schedule_fetch(self, source):
        if (timestamp("2026-07-08 09:24:58") <= self.clock.timestamp_ns()
                < timestamp("2026-07-08 09:25:00")):
            raise AssertionError("Auction execution must not fetch a market snapshot")
        return super()._schedule_fetch(source)


class MissingExecutionPriceStrategy(CachedExecutionPriceStrategy):
    def __init__(self, config, *, missing_instruments, available_at_ns):
        super().__init__(config)
        self._test_missing_instruments = set(missing_instruments)
        self._test_available_at_ns = available_at_ns

    def _select_opening_auction_reference(self, instrument_id):
        if (instrument_id in self._test_missing_instruments
                and timestamp("2026-07-08 09:24:58") <= self.clock.timestamp_ns()
                < self._test_available_at_ns):
            return None, {}
        return super()._select_opening_auction_reference(instrument_id)


class AuctionCleanupPolicyTest(unittest.TestCase):
    @staticmethod
    def morning_rows(rows):
        morning = [r for r in rows if r.ts_init <= timestamp("2026-07-08 09:30:00")]
        return morning + [replace(r, ts_init=timestamp("2026-07-08 09:30:10"),
                                  ts_event=timestamp("2026-07-08 09:30:10"))
                          for r in morning if r.ts_init == timestamp("2026-07-08 09:30:00")]

    def run_auction(self, *, skip, missing=(), available_at="09:25:00.200", skip_exit=False):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\ntasks:\n"
                            "  auction_execution:\n    skip_entry_cleanup: " +
                            ("true" if skip else "false") + "\n    skip_exit_cleanup: " +
                            ("true" if skip_exit else "false") + "\n")
            engine, strategy, _, _ = replay_fixture(continuous=True,
                transform_rows=self.morning_rows,
                strategy_type=lambda c: MissingExecutionPriceStrategy(c,
                    missing_instruments=missing,
                    available_at_ns=timestamp(f"2026-07-08 {available_at}")),
                transform_config=lambda c: config_replace(c, task_orchestration_config=str(path),
                    auction_execution_time="09:24:58", auction_submit_cutoff="09:25:00",
                    cancel_reconverge_delay_secs=1.6))
            try:
                engine.run()
                records = [r for r in strategy.orchestration.engine.records if r.task_id == "auction_execution"]
                self.assertFalse([r for r in strategy.orchestration.engine.records
                                  if r.action in {"COMPONENT_ERROR", "TRIGGER_ERROR"}])
                orders = [o for o in engine.cache.orders() if o.tags is not None and "opening_auction" in o.tags]
                return records, orders
            finally:
                engine.dispose()

    def test_cached_prices_submit_at_admission_without_fetch_or_entry_cleanup_delay(self):
        records, orders = self.run_auction(skip=True)
        entry = next(r for r in records if r.action == "CLEANUP_SKIPPED")
        admitted = next(r for r in records if r.action == "ADMITTED")
        self.assertEqual(entry.evidence["kind"], "entry")
        self.assertEqual(entry.now_ns, admitted.now_ns)
        self.assertFalse([r for r in records if r.action == "CLEANUP_COMPLETED" and r.evidence["kind"] == "entry"])
        self.assertTrue(orders, records)
        self.assertEqual({o.ts_init for o in orders}, {admitted.now_ns})
        self.assertTrue(all(timestamp("2026-07-08 09:24:58") <= o.ts_init < timestamp("2026-07-08 09:25:00") for o in orders))
        self.assertTrue([r for r in records if r.action == "CLEANUP_COMPLETED" and r.evidence["kind"] == "exit"])

    def test_accepted_orders_finish_before_cutoff_and_skip_exit_cleanup(self):
        records, orders = self.run_auction(skip=True, skip_exit=True)
        outcome = next(r for r in records if r.action == "OUTCOME_DECIDED")
        self.assertEqual(outcome.reason, "auction_orders_accepted")
        self.assertLess(outcome.now_ns, timestamp("2026-07-08 09:25:00"))
        self.assertEqual([r.evidence["kind"] for r in records if r.action == "CLEANUP_SKIPPED"],
                         ["entry", "exit"])
        self.assertTrue(orders)
        self.assertTrue(all(o.filled_qty == o.quantity for o in orders))

    def test_original_entry_cleanup_delay_can_exhaust_the_two_second_window(self):
        records, orders = self.run_auction(skip=False)
        self.assertEqual(orders, [])
        self.assertFalse([r for r in records if r.action == "CLEANUP_SKIPPED"])
        self.assertTrue([r for r in records if r.action == "OUTCOME_DECIDED" and r.reason == "proposal_expired"])

    def test_all_missing_prices_skip_orders_without_a_price_wait_and_still_clean_up(self):
        records, orders = self.run_auction(skip=True, missing=(A, B))
        self.assertEqual(orders, [])
        self.assertTrue([r for r in records if r.action == "PROGRESS" and r.reason == "auction_orders_tracked"])
        self.assertFalse([r for r in records if r.reason == "auction_execution_price_pending"])
        self.assertTrue([r for r in records if r.action == "OUTCOME_DECIDED" and r.reason == "proposal_expired"])
        self.assertEqual([r.evidence["kind"] for r in records if r.action == "CLEANUP_SKIPPED"], ["entry"])
        self.assertTrue([r for r in records if r.action == "CLEANUP_COMPLETED" and r.evidence["kind"] == "exit"])
        terminal = next(r for r in records if r.action == "TERMINATED")
        self.assertEqual((terminal.reason, terminal.evidence["state"]), ("proposal_expired", "expired"))

    def test_missing_price_only_skips_that_stock_and_other_stock_submits_at_admission(self):
        for missing, submitted in ((A, B), (B, A)):
            with self.subTest(missing=missing):
                records, orders = self.run_auction(skip=True, missing=(missing,))
                admitted = next(r for r in records if r.action == "ADMITTED")
                self.assertEqual({str(o.instrument_id) for o in orders}, {submitted})
                self.assertEqual({o.ts_init for o in orders}, {admitted.now_ns})
                self.assertFalse([r for r in records if r.reason == "auction_execution_price_pending"])

    def test_skipped_stock_can_submit_when_its_price_recovers_within_window(self):
        records, orders = self.run_auction(skip=True, missing=(A,), available_at="09:24:59")
        submitted = {str(o.instrument_id): o.ts_init for o in orders}
        admitted = next(r for r in records if r.action == "ADMITTED")
        self.assertEqual(submitted[B], admitted.now_ns)
        self.assertGreaterEqual(submitted[A], timestamp("2026-07-08 09:24:59"))
        self.assertLess(submitted[A], timestamp("2026-07-08 09:25:00"))
        self.assertFalse([r for r in records if r.reason == "auction_execution_price_pending"])

    def test_default_submission_window_includes_start_and_excludes_cutoff(self):
        engine, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(c,
            auction_execution_time="09:24:58", auction_submit_cutoff="09:25:00"))
        try:
            for clock_time, allowed in (("09:24:57.999999999", False),
                                        ("09:24:58", True),
                                        ("09:24:59.999999999", True),
                                        ("09:25:00", False),
                                        ("09:25:00.000000001", False)):
                with self.subTest(clock_time=clock_time):
                    strategy.clock.set_time(timestamp(f"2026-07-08 {clock_time}"))
                    self.assertEqual(strategy._within_auction_submit_time(), allowed)
        finally:
            engine.dispose()
