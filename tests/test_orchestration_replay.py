from __future__ import annotations

import asyncio
from datetime import date
from dataclasses import replace
from decimal import Decimal
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
from concurrent.futures import ThreadPoolExecutor

from msgspec.structs import replace as config_replace
import uvloop

from tests.test_opening_auction import replay_fixture, FixedPlanner, timestamp
from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
from strategies.model_target_planners.base import ModelTargetPlan, TargetInfo, TargetContext
from strategies.replanning import planning_json


class WeightedPlanner(FixedPlanner):
    def plan(self, request):
        plan = super().plan(request)
        return replace(plan, targets=[replace(t, weight=.1) for t in plan.targets])


def morning_rows(rows):
    base = [r for r in rows if r.ts_init == timestamp("2026-07-08 09:30:00")]
    rows = [r for r in rows if r.ts_init <= timestamp("2026-07-08 09:30:00")]
    # Preserve a real deficit after the first continuous book is matched. Timer
    # arbitration intentionally runs at cycle boundaries, after market callbacks.
    rows = [replace(r, ask_size=100, bid_size=100) if r in base else r for r in rows]
    # Later quotes cross the correction limit prices so this fixture tests
    # post-fill cleanup/completion rather than an indefinitely resting bid.
    return rows + [replace(r, bid_price=r.bid_price - (.05 if second >= 3 else 0),
                           ask_price=r.ask_price - (.05 if second >= 3 else 0), ts_init=timestamp(f"2026-07-08 09:30:{second:02}"),
                           ts_event=timestamp(f"2026-07-08 09:30:{second:02}"))
                   for second in range(1, 12) for r in base]


class OrchestrationReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    def run_morning(self, *, exposure=True, deny_second=False, cleanup_delay=0):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: " +
                            ("true" if exposure else "false") + "\n")
            engine, strategy, _, _ = replay_fixture(
                continuous=True, final_results=False, starting_cash=100000,
                transform_rows=morning_rows,
                transform_config=lambda c: config_replace(
                    c, task_orchestration_config=str(path), cancel_reconverge_delay_secs=cleanup_delay))
            planner = WeightedPlanner()
            strategy._target_planner = planner
            try:
                engine.run()
                errors = [r for r in strategy.orchestration.engine.records
                          if r.action in {"COMPONENT_ERROR", "TRIGGER_ERROR"}]
                self.assertEqual(errors, [])
                orders = [(str(o.instrument_id), o.side.name, str(o.quantity), o.ts_init,
                           strategy._order_target_versions.get(str(o.client_order_id), ""))
                          for o in engine.cache.orders()]
                records = [(r.now_ns, r.action, r.task_id, r.reason, dict(r.evidence))
                           for r in strategy.orchestration.engine.records]
                return orders, records, len(planner.requests)
            finally:
                engine.dispose()

    def test_morning_correction_trades_only_deficient_side(self):
        orders, records, _ = self.run_morning()
        corrections = [o for o in orders if o[3] >= timestamp("2026-07-08 09:30:00")]
        self.assertTrue(corrections, records)
        self.assertTrue(all(o[1] == "BUY" for o in corrections))
        self.assertTrue(all(timestamp("2026-07-08 09:30:00") <= o[3] <
                            timestamp("2026-07-08 14:30:00") for o in corrections))
        completed = [r for r in records if r[2] == "exposure_correction"
                     and r[3] == "exposure_within_tolerance"]
        self.assertTrue(completed, records)

    def test_disabled_task_does_not_trade_before_normal_window(self):
        orders, records, _ = self.run_morning(exposure=False)
        self.assertFalse([o for o in orders if o[3] >= timestamp("2026-07-08 09:30:00")])
        self.assertFalse([r for r in records if r[2] == "exposure_correction"])

    def test_normal_execution_acknowledges_first_depth_after_actual_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\n")
            engine, strategy, _, _ = replay_fixture(
                continuous=True, starting_cash=100000,
                transform_config=lambda c: config_replace(
                    c, task_orchestration_config=str(path), cancel_reconverge_delay_secs=0))
            try:
                engine.run()
                self.assertTrue(strategy._depth_converged_today)
                # A raw executor call outside a framework callback is forbidden.
                from strategies.convergence import ConvergenceTrigger
                with self.assertRaisesRegex(RuntimeError, "active orchestration owner"):
                    strategy.orchestration.execute(trigger=ConvergenceTrigger.ORDER_BOOK_DEPTH)
            finally:
                engine.dispose()

    def test_complete_auction_goal_acknowledges_depth_without_new_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\n")
            engine, strategy, _, _ = replay_fixture(
                continuous=True, starting_cash=100000,
                transform_rows=lambda rows: [replace(r, bid_size=100000, ask_size=100000)
                                             if r.kind == "auction" else r for r in rows],
                transform_config=lambda c: config_replace(
                    c, task_orchestration_config=str(path), cancel_reconverge_delay_secs=0))
            try:
                engine.run()
                self.assertTrue(strategy._depth_converged_today)
                self.assertEqual(len(engine.cache.orders()), 2)
                self.assertFalse([r for r in strategy.orchestration.engine.records
                                  if r.task_id == "normal_execution"])
            finally:
                engine.dispose()

    def test_queued_depth_does_not_acknowledge_a_completed_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: false\n")
            engine, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            try:
                from strategies.convergence import ConvergenceTrigger
                now = timestamp("2026-07-08 14:30:00")
                strategy.clock.set_time(now)
                strategy._last_market_data_ts_event = now
                self.assertTrue(strategy._within_execution_window())
                self.assertFalse(strategy.orchestration.execution_requested(
                    ConvergenceTrigger.ORDER_BOOK_DEPTH))
                self.assertFalse(strategy._depth_converged_today)
            finally:
                engine.dispose()

    def test_identical_replay_has_identical_decisions_and_orders(self):
        self.assertEqual(self.run_morning(), self.run_morning())

    def test_delayed_cleanup_preserves_exposure_completion_evidence(self):
        _, records, _ = self.run_morning(cleanup_delay=0.1)
        completed = [r for r in records if r[1] == "TERMINATED" and r[2] == "exposure_correction"]
        self.assertTrue(completed, records)
        for terminal in completed:
            self.assertEqual(terminal[3], "exposure_within_tolerance")
            self.assertEqual(terminal[4]["state"], "succeeded")
            self.assertLessEqual(abs(Decimal(terminal[4]["deviation"])), Decimal("0.05"))
            self.assertGreater(Decimal(terminal[4]["denominator"]), 0)
            decided = [r for r in records if r[1] == "OUTCOME_DECIDED" and r[2] == terminal[2]
                       and r[3:] == terminal[3:] and r[0] < terminal[0]]
            self.assertTrue(decided, records)

    def test_unknown_configuration_fails_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  tolreance: 0.05\n")
            with self.assertRaises(ValueError):
                OrchestrationConfig.load(str(path))

    def test_operation_permit_does_not_leak_to_another_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\n")
            engine, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            try:
                bridge = strategy.orchestration
                # Isolate the thread-local permit from account/market setup.
                with patch.object(bridge.engine, "operation_phase", return_value="execute"), \
                        bridge.operations(), ThreadPoolExecutor(max_workers=1) as pool:
                    bridge.reject_unowned_operation()
                    future = pool.submit(bridge.reject_unowned_operation)
                    with self.assertRaisesRegex(RuntimeError, "active orchestration owner"):
                        future.result()
                with self.assertRaises(RuntimeError):
                    bridge.reject_unowned_operation()
            finally:
                engine.dispose()

    def run_bootstrap_archive(self, *, corrupt=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: true\n")
            engine, strategy, planner, _ = replay_fixture(
                continuous=True, final_results=False, starting_cash=100000,
                transform_rows=morning_rows,
                transform_config=lambda c: config_replace(c, task_orchestration_config=str(path),
                                                          cancel_reconverge_delay_secs=0))
            day = date(2026, 7, 8)
            plan = ModelTargetPlan(day, day, [], "empty_formal_target", request_id="saved",
                                   total_asset=100000, investable_asset=100000)
            archive = {"version": strategy._plan_version(plan), "quantities": {},
                       "reason": plan.reason, "signal_date": day,
                       "plan": None if corrupt else planning_json(plan)}
            strategy.configure_planning_archive(lambda *_args: None, lambda _day: archive)
            try:
                engine.run()
                return (list(engine.cache.orders()), len(planner.requests),
                        [(r.task_id, r.action, r.reason) for r in strategy.orchestration.engine.records])
            finally:
                engine.dispose()

    def test_bootstrap_restores_empty_formal_target_without_optimizer(self):
        orders, requests, records = self.run_bootstrap_archive()
        self.assertEqual(orders, [])
        self.assertEqual(requests, 0)
        self.assertIn(("bootstrap", "TERMINATED", "initialized_from_business_facts"), records)

    def test_corrupt_archive_keeps_bootstrap_gate_closed(self):
        orders, requests, records = self.run_bootstrap_archive(corrupt=True)
        self.assertEqual(orders, [])
        self.assertEqual(requests, 0)
        self.assertTrue(any("bootstrap_load_failed" in reason for _, _, reason in records))
        self.assertFalse(any(task != "bootstrap" and action == "ADMITTED"
                             for task, action, _ in records))

    def test_correction_increases_small_buy_to_legal_notional_in_real_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 2\nexposure:\n  enabled: true\n")
            engine, strategy, planner, _ = replay_fixture(
                continuous=True, final_results=False, auction_enabled=False,
                starting_cash=100000, continuous_buy_min_notional=17000,
                transform_rows=morning_rows,
                transform_config=lambda c: config_replace(c, task_orchestration_config=str(path),
                                                          cancel_reconverge_delay_secs=0))
            day = date(2026, 7, 8)
            plan = ModelTargetPlan(day, day, [TargetInfo(
                "000002.SZ", .2, 1000, TargetContext(), None, "000002.SZ.QMT", False)],
                "restored_target", request_id="saved", total_asset=100000, investable_asset=100000)
            archive = {"version": strategy._plan_version(plan), "quantities": {"000002.SZ.QMT": 1000},
                       "reason": plan.reason, "signal_date": day, "plan": planning_json(plan)}
            strategy.configure_planning_archive(lambda *_args: None, lambda _day: archive)
            try:
                engine.run()
                orders = engine.cache.orders()
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[0].quantity.as_decimal(), Decimal(900))
                self.assertGreater(orders[0].quantity.as_decimal() * orders[0].price.as_decimal(),
                                   Decimal(17000))
                self.assertEqual(orders[0].filled_qty.as_decimal(), Decimal(900))
                self.assertEqual(len(planner.requests), 0)
                self.assertTrue(any(r.action == "TERMINATED" and r.task_id == "exposure_correction"
                                    and r.reason == "exposure_within_tolerance"
                                    for r in strategy.orchestration.engine.records))
            finally:
                engine.dispose()

