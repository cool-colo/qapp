from __future__ import annotations

import asyncio
from datetime import date
from dataclasses import replace
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor

from msgspec.structs import replace as config_replace
import uvloop

from tests.test_opening_auction import replay_fixture, FixedPlanner, timestamp
from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
from strategies.model_target_planners.base import ModelTargetPlan
from strategies.replanning import planning_json


class WeightedPlanner(FixedPlanner):
    def plan(self, request):
        plan = super().plan(request)
        return replace(plan, targets=[replace(t, weight=.1) for t in plan.targets])


def morning_rows(rows):
    base = [r for r in rows if r.ts_init == timestamp("2026-07-08 09:30:00")]
    rows = [r for r in rows if r.ts_init <= timestamp("2026-07-08 09:30:00")]
    return rows + [replace(r, ts_init=timestamp(f"2026-07-08 09:30:{second:02}"),
                           ts_event=timestamp(f"2026-07-08 09:30:{second:02}"))
                   for second in range(1, 12) for r in base]


class OrchestrationReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    def run_morning(self, *, exposure=True, deny_second=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 1\nexposure:\n  enabled: " +
                            ("true" if exposure else "false") + "\n")
            engine, strategy, _, _ = replay_fixture(
                continuous=True, final_results=False, starting_cash=100000,
                transform_rows=morning_rows,
                transform_config=lambda c: config_replace(
                    c, task_orchestration_config=str(path), cancel_reconverge_delay_secs=0))
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

    def test_identical_replay_has_identical_decisions_and_orders(self):
        self.assertEqual(self.run_morning(), self.run_morning())

    def test_unknown_configuration_fails_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 1\nexposure:\n  tolreance: 0.05\n")
            with self.assertRaises(ValueError):
                OrchestrationConfig.load(str(path))

    def test_operation_permit_does_not_leak_to_another_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.yaml"
            path.write_text("schema_version: 1\n")
            engine, strategy, _, _ = replay_fixture(transform_config=lambda c: config_replace(
                c, task_orchestration_config=str(path)))
            try:
                bridge = strategy.orchestration
                with bridge.operations(), ThreadPoolExecutor(max_workers=1) as pool:
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
            path.write_text("schema_version: 1\nexposure:\n  enabled: true\n")
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

