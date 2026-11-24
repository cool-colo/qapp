from dataclasses import replace
from contextlib import nullcontext
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
import unittest

from nautilus_trader.adapters.qmt.common import parse_equity
from unittest.mock import Mock

from trading_orchestration.adapters.exposure_snapshot_provider import ExposureSnapshotProvider
from trading_orchestration.components.allocations.exposure_order_admission import ExposureOrderAdmission
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.components.planners.remaining_target_exposure_planner import RemainingTargetExposurePlanner
from trading_orchestration.configuration.exposure_config import ExposureConfig
from trading_orchestration.contexts.exposure_context import (
    ExposureContext, FormalTarget, InstrumentSnapshot, PendingExposureOrder,
)
from strategies.order_splitter.notional_order_splitter import NotionalOrderSplitter
from trading_orchestration.tasks.exposure_correction_task import ExposureCorrectionTask
from trading_orchestration.components.outcomes.exposure_outcome_policy import ExposureOutcomePolicy


def snapshot(*, quantity='74000', target='80000', weight='0.8', assets='1000000',
             minimum='10000', odd_lot=False):
    item = InstrumentSnapshot(D(quantity), D(10), D(quantity),
                              minimum_buy_notional=D(minimum), allow_odd_lot_sell=odd_lot)
    formal = FormalTarget('v1', '2026-07-08', {'A': D(target)}, {'A': D(weight)}, D(0), D(assets))
    return ExposureContext(0, formal.trading_date, 0, D(assets), D(assets) - D(quantity) * 10,
                           {'A': item}, formal)


class ExposureQuantityRulesTest(unittest.TestCase):
    def setUp(self):
        self.planner = RemainingTargetExposurePlanner(ExposureConfig())
        self.admission = ExposureOrderAdmission()

    def odd_context(self, **changes):
        return snapshot(quantity='90', target='0', weight='0', assets='10000', minimum='0',
                        **{'odd_lot': True, **changes})

    def test_minimum_buy_selects_larger_legal_order_and_admission_preserves_it(self):
        ctx = snapshot()
        plan = self.planner.plan(ctx)
        self.assertEqual(plan.status, 'planned')
        self.assertEqual(plan.quantities['A'], D(80000))
        self.assertEqual(plan.notional, D(60000))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(6000), order_price=D(10)), D(6000))
        # Reaching the exact monetary minimum remains illegal, as in the executor.
        self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(1000), order_price=D(10)), D(0))
        after = replace(ctx, instruments={'A': replace(ctx.instruments['A'], quantity=D(75100))})
        self.assertEqual(ExposureAssessor().assess(after.position_snapshot()).status, 'satisfied')

    def test_only_physical_admission_uses_actual_limit_price(self):
        ctx = snapshot(target='75100')
        plan = self.planner.plan(ctx)
        self.assertEqual(plan.quantities['A'], D(75100))
        for price, quantity, expected in (('9', 1100, 0), ('10.01', 1000, 1000)):
            with self.subTest(price=price):
                self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(quantity),
                                                         order_price=D(price)), D(expected))

    def test_estimates_ignore_fees_but_respect_cash_and_formal_remainder(self):
        for ctx in (replace(snapshot(), free_cash=D(10000)), snapshot(target='75000')):
            with self.subTest(ctx=ctx):
                self.assertEqual(self.planner.plan(ctx).status, 'replan')
        ctx = replace(snapshot(), free_cash=D(11000))
        self.assertEqual(self.planner.plan(ctx).quantities['A'], D(75100))
        self.assertEqual(self.admission.quantity(snapshot(target='75000'), 'A', 'buy', D(1100),
                                                 order_price=D(10)), D(0))

    def test_planner_selects_lot_closest_to_center(self):
        for weight, expected in (('0.8004', '80000'), ('0.8006', '80100')):
            with self.subTest(weight=weight):
                plan = self.planner.plan(snapshot(weight=weight, target='81000'))
                self.assertEqual(plan.quantities['A'], D(expected))

    def test_started_correction_can_continue_inside_tolerance(self):
        ctx = snapshot(quantity='76000')
        self.assertEqual(ExposureAssessor().assess(ctx.position_snapshot()).status, 'satisfied')
        self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(4000), order_price=D(10)), D(4000))

    def test_pending_center_orders_are_retained_after_partial_fill_enters_tolerance(self):
        plan = self.planner.plan(snapshot())
        ctx = replace(snapshot(quantity='76000'), pending_orders=1,
                      pending=(PendingExposureOrder('A', 'buy', D(4000)),))
        reconcile = Mock()
        bridge = SimpleNamespace(
            host=SimpleNamespace(automatic_replanning_allowed=lambda: True,
                                 _clock_date=lambda: date(2026, 7, 8),
                                 _reconcile_unfilled_orders=reconcile),
            config=SimpleNamespace(exposure=ExposureConfig()), in_windows=lambda _: True,
            snapshot_provider=SimpleNamespace(capture_positions=lambda: ctx.position_snapshot()),
            operations=lambda **kwargs: nullcontext())
        task = ExposureCorrectionTask(bridge, assessor=ExposureAssessor(), planner=self.planner,
                                      executor_factory=Mock, risk_planner_factory=Mock,
                                      outcome_policy=Mock())
        task.active_plan = plan
        decision = task.advance(None)
        self.assertEqual(decision.reason, 'awaiting_corrective_orders')
        self.assertFalse(decision.requires_stable)
        reconcile.assert_called_once_with(date(2026, 7, 8))

    def test_task_rechecks_exposure_with_fresh_planning_snapshot_before_execution(self):
        detected = snapshot().position_snapshot()
        fresh = snapshot(quantity='80000')
        provider = SimpleNamespace(capture_positions=Mock(return_value=detected),
                                   capture_planning=Mock(return_value=fresh))
        bridge = SimpleNamespace(host=SimpleNamespace(automatic_replanning_allowed=lambda: True),
            config=SimpleNamespace(exposure=ExposureConfig()), in_windows=lambda _: True,
            snapshot_provider=provider, exposure_block=None)
        executor = Mock()
        task = ExposureCorrectionTask(bridge, assessor=ExposureAssessor(), planner=self.planner,
            executor_factory=lambda: executor, risk_planner_factory=Mock(), outcome_policy=ExposureOutcomePolicy())
        decision = task.advance(None)
        self.assertEqual(decision.reason, 'within_tolerance')
        provider.capture_positions.assert_called_once_with()
        provider.capture_planning.assert_called_once_with()
        executor.apply.assert_not_called()

    def test_larger_minimum_cannot_cross_opposite_tolerance_boundary(self):
        ctx = snapshot(weight='0.7402')
        config = ExposureConfig(tolerance=D('0.0001'))
        self.assertEqual(RemainingTargetExposurePlanner(config).plan(ctx).status, 'replan')
        self.assertEqual(ExposureOrderAdmission(config.tolerance).quantity(ctx, 'A', 'buy', D(1100),
                                                                          order_price=D(10)), D(0))

    def test_minimum_and_pending_buys_are_checked_together(self):
        ctx = replace(snapshot(), pending_orders=1, pending=(PendingExposureOrder('A', 'buy', D(100)),))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(6000), order_price=D(10)), D(5900))
        ctx = replace(ctx, pending=(PendingExposureOrder('A', 'buy', D(5000)),))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'buy', D(1100), order_price=D(10)), D(0))

    def test_odd_lot_liquidation_survives_planning_admission_and_splitter(self):
        ctx = self.odd_context()
        plan = self.planner.plan(ctx)
        self.assertEqual(plan.status, 'planned')
        self.assertEqual(plan.available_notional, D(900))
        self.assertEqual(plan.quantities['A'], D(0))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(90), order_price=D(10)), D(90))
        instrument = parse_equity('000001.SZ', {}, 0, 0)
        self.assertEqual(NotionalOrderSplitter(D(100000)).split(
            instrument, D(90), D(10), allow_odd_lot=True), [D(90)])

    def test_odd_lot_requires_permission_zero_target_and_full_sellability(self):
        ctx = self.odd_context()
        cases = [self.odd_context(odd_lot=False),
                 replace(ctx, target=replace(ctx.target, quantities={'A': D(10)})),
                 replace(ctx, instruments={'A': replace(ctx.instruments['A'], sellable=D(80))})]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(self.planner.plan(value).status, 'replan')
                self.assertEqual(self.admission.quantity(value, 'A', 'sell', D(90), order_price=D(10)), D(0))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(80), order_price=D(10)), D(0))

    def test_odd_lot_cannot_cross_tolerance_boundary(self):
        ctx = self.odd_context()
        ctx = replace(ctx, target=replace(ctx.target, weights={'A': D('0.08')}))
        config = ExposureConfig(tolerance=D('0.005'))
        self.assertEqual(RemainingTargetExposurePlanner(config).plan(ctx).status, 'replan')
        self.assertEqual(ExposureOrderAdmission(config.tolerance).quantity(ctx, 'A', 'sell', D(90),
                                                                          order_price=D(10)), D(0))

    def test_liquidation_capacity_includes_residual_above_regular_minimum(self):
        ctx = snapshot(quantity='190', target='0', weight='0', assets='10000', odd_lot=True)
        self.assertEqual(self.planner.plan(ctx).quantities['A'], D(0))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(190), order_price=D(10)), D(190))

    def test_liquidation_of_last_slice_counts_pending_without_double_subtracting_sellable(self):
        ctx = snapshot(quantity='190', target='0', weight='0', assets='10000', odd_lot=True)
        ctx = replace(ctx, instruments={'A': replace(ctx.instruments['A'], sellable=D(90))},
                      pending_orders=1, pending=(PendingExposureOrder('A', 'sell', D(100)),))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(90), order_price=D(10)), D(90))
        ctx = replace(ctx, pending_orders=2, pending=ctx.pending + (PendingExposureOrder('A', 'sell', D(90)),))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(90), order_price=D(10)), D(0))

    def test_small_regular_correction_is_preferred_over_unneeded_liquidation(self):
        ctx = snapshot(quantity='290', target='0', weight='0.19', assets='10000', odd_lot=True)
        self.assertEqual(self.planner.plan(ctx).quantities['A'], D(190))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(290), order_price=D(10)), D(100))

    def test_board_with_one_share_increment_keeps_its_minimum_and_liquidation_exception(self):
        ctx = self.odd_context()
        ctx = replace(ctx, instruments={'A': replace(ctx.instruments['A'], increment=D(1), minimum=D(200))})
        self.assertEqual(self.planner.plan(ctx).quantities['A'], D(0))
        self.assertEqual(self.admission.quantity(ctx, 'A', 'sell', D(90), order_price=D(10)), D(90))

    def test_snapshot_transmits_instrument_odd_lot_rule(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                instrument = parse_equity('000001.SZ', {}, 0, 0)
                # Instrument reference data is the authority for this permission.
                info = dict(instrument.info)
                info['limit_order_quantity_rules'] = {**info['limit_order_quantity_rules'],
                                                      'allow_odd_lot_sell': enabled}
                reference = SimpleNamespace(id=instrument.id, info=info)
                iid = str(instrument.id)
                host = SimpleNamespace(
                    clock=SimpleNamespace(timestamp_ns=lambda: 10), _clock_date=lambda: date(2026, 7, 8),
                    trading_state=SimpleNamespace(formal_model=None), _execution_activity=0,
                    _held_instrument_ids=lambda: {iid}, _target_quantities={},
                    cache=SimpleNamespace(instrument=lambda _: reference), _current_quantity=lambda _: D(90),
                    _market_status={iid: SimpleNamespace(last_price=10, ts_event=10, market_status=None)},
                    market_snapshots=SimpleNamespace(latest_snapshot=lambda: {}), _depth_books={},
                    _planning_permissions=lambda *args, **kwargs: (True, True),
                    _limit_price=Mock(side_effect=AssertionError('snapshot must not price orders')),
                    planning_sellable_quantity=lambda *args: 90,
                    config=SimpleNamespace(continuous_buy_min_notional=D(10000)),
                    _instrument_ids=[instrument.id], _nautilus_free_cash=lambda: D(9100),
                    portfolio=SimpleNamespace(account=lambda **kwargs: SimpleNamespace(
                        balance_total=lambda: SimpleNamespace(as_decimal=lambda: D(9100)))),
                    replanning_orders=lambda: [],
                )
                value = ExposureSnapshotProvider(host, ExposureConfig()).capture_order(iid)
                self.assertEqual(value.instruments[iid].allow_odd_lot_sell, enabled)
                self.assertEqual(value.instruments[iid].sellable, D(90))
                host._limit_price.assert_not_called()
