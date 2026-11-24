from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import unittest

from nautilus_trader.adapters.qmt.common import parse_equity
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Quantity

from strategies.model_target_planners.base import ModelTargetPlan, TargetContext, TargetInfo
from trading_orchestration.adapters.exposure_snapshot_provider import ExposureSnapshotProvider
from trading_orchestration.components.allocations.exposure_order_admission import ExposureOrderAdmission
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.components.planners.remaining_target_exposure_planner import RemainingTargetExposurePlanner
from trading_orchestration.components.validators.exposure_validator import ExposureValidator
from trading_orchestration.configuration.exposure_config import ExposureConfig


DAY = date(2026, 7, 8)
A, B, C = '000001.SZ.QMT', '000002.SZ.QMT', '000003.SZ.QMT'


class ExposureSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=int(datetime(2026, 7, 8, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()) * 10**9)
        self.quantities = {A: Decimal(100), B: Decimal(0), C: Decimal(0)}
        self.funds = {'balance': Decimal(9000), 'free': Decimal(9000)}
        self.orders = []
        instruments = [parse_equity(code, {}, 0, 0) for code in ('000001.SZ', '000002.SZ', '000003.SZ')]
        self.instruments = {str(instrument.id): instrument for instrument in instruments}
        plan = ModelTargetPlan(DAY, DAY, [
            TargetInfo('000001.SZ', .1, 100, TargetContext(), None, A, False),
            TargetInfo('000002.SZ', .2, 400, TargetContext(), None, B, False),
            TargetInfo('000003.SZ', .1, 100, TargetContext(), None, C, False),
        ], 'formal', total_asset=10000, investable_asset=10000)
        self.host = SimpleNamespace(
            clock=SimpleNamespace(timestamp_ns=lambda: self.clock.now), _clock_date=lambda: DAY,
            trading_state=SimpleNamespace(formal_model=plan), _plan_version=lambda _: 'formal',
            _execution_activity=0, _instrument_ids=[instrument.id for instrument in instruments],
            _held_instrument_ids=lambda: {iid for iid, qty in self.quantities.items() if qty != 0},
            _current_quantity=Mock(side_effect=lambda iid: self.quantities[str(iid)]),
            _market_status={iid: SimpleNamespace(last_price=10, ts_event=self.clock.now, market_status=None)
                            for iid in (A, B)}, _depth_books={},
            market_snapshots=SimpleNamespace(latest_snapshot=lambda: {}),
            cache=SimpleNamespace(instrument=Mock(side_effect=lambda iid: self.instruments.get(str(iid)))),
            portfolio=SimpleNamespace(account=lambda **kwargs: SimpleNamespace(
                balance_total=lambda: SimpleNamespace(as_decimal=lambda: self.funds['balance']))),
            _nautilus_free_cash=Mock(side_effect=lambda: self.funds['free']),
            _planning_permissions=Mock(return_value=(True, True)),
            planning_sellable_quantity=Mock(side_effect=lambda iid, day: int(self.quantities[iid])),
            _limit_price=Mock(side_effect=AssertionError('snapshot must not price orders')),
            config=SimpleNamespace(continuous_buy_min_notional=Decimal(0)),
            replanning_orders=lambda: list(self.orders),
        )
        self.config = ExposureConfig(quote_max_age_secs=15.0)
        self.provider = ExposureSnapshotProvider(self.host, self.config)
        self.assessor = ExposureAssessor(self.config.tolerance)
        self.planner = RemainingTargetExposurePlanner(self.config)

    def test_detection_ignores_unheld_targets_and_reads_no_order_inputs(self):
        positions = self.provider.capture_positions()
        self.assertEqual(set(positions.positions), {A})
        self.assertEqual(positions.total_assets, Decimal(10000))
        self.assertEqual(positions.quality_error, '')
        self.assertEqual(self.assessor.assess(positions).direction, 'buy')
        self.host.cache.instrument.assert_not_called()
        self.host._planning_permissions.assert_not_called()
        self.host.planning_sellable_quantity.assert_not_called()
        self.host._nautilus_free_cash.assert_not_called()
        self.host._limit_price.assert_not_called()

    def test_stale_holding_still_blocks_detection_at_fifteen_seconds(self):
        self.host._market_status[A].ts_event = self.clock.now - 15_000_000_000
        self.assertEqual(self.provider.capture_positions().quality_error, '')
        self.clock.now += 1
        positions = self.provider.capture_positions()
        self.assertEqual(positions.quality_error, f'missing_fresh_price:{A}')
        self.assertEqual(self.assessor.assess(positions).status, 'unknown')

    def test_planning_can_use_fresh_candidates_without_waiting_for_all_targets(self):
        snapshot = self.provider.capture_planning()
        self.assertEqual(snapshot.quality_error, '')
        self.assertEqual(snapshot.instruments[C].quality_error, f'missing_fresh_price:{C}')
        plan = self.planner.plan(snapshot)
        self.assertEqual(plan.status, 'planned')
        self.assertEqual(plan.quantities[B], Decimal(300))
        self.assertEqual(plan.excluded[C], f'missing_fresh_price:{C}')
        self.assertTrue(ExposureValidator().validate(self.provider.capture_planning(), plan))
        self.host._limit_price.assert_not_called()

    def test_planning_waits_if_missing_candidates_are_needed(self):
        del self.host._market_status[B]
        snapshot = self.provider.capture_planning()
        self.assertEqual(snapshot.quality_error, '')
        plan = self.planner.plan(snapshot)
        self.assertEqual(plan.status, 'unknown')
        self.assertEqual(plan.reason, f'missing_fresh_price:{B}')

    def test_planning_revalues_positions_and_rechecks_direction(self):
        detected = self.provider.capture_positions()
        self.assertEqual(self.assessor.assess(detected).direction, 'buy')
        self.clock.now += 1_000_000_000
        self.quantities[A] = Decimal(800)
        self.funds.update(balance=Decimal(2000), free=Decimal(2000))
        self.host._market_status[A].ts_event = self.clock.now
        del self.host._market_status[B]
        self.host._execution_activity += 1
        snapshot = self.provider.capture_planning()
        self.assertGreater(snapshot.now_ns, detected.now_ns)
        self.assertEqual(snapshot.execution_revision, detected.execution_revision + 1)
        self.assertEqual(detected.positions[A].quantity, Decimal(100))
        self.assertEqual(snapshot.instruments[A].quantity, Decimal(800))
        self.assertEqual(set(snapshot.instruments), {A})
        plan = self.planner.plan(snapshot)
        self.assertEqual(plan.direction, 'sell')
        self.assertEqual(plan.quantities[A], Decimal(400))

    def test_order_admission_has_no_dependency_on_unrelated_target_prices(self):
        snapshot = self.provider.capture_order(B)
        self.assertEqual(set(snapshot.instruments), {A, B})
        admitted = ExposureOrderAdmission().quantity(snapshot, B, 'buy', Decimal(300), order_price=Decimal(10))
        self.assertEqual(admitted, Decimal(300))

    def test_pending_unheld_order_needs_a_price_only_for_physical_admission(self):
        self.orders.append(SimpleNamespace(client_order_id=ClientOrderId('pending'),
            instrument_id=self.instruments[C].id, side=OrderSide.BUY, leaves_qty=Quantity.from_int(100)))
        positions = self.provider.capture_positions()
        self.assertEqual(positions.quality_error, '')
        self.assertEqual(positions.pending_orders, 1)
        snapshot = self.provider.capture_order(B)
        self.assertEqual(snapshot.instruments[C].quality_error, f'missing_fresh_price:{C}')
        self.assertEqual(ExposureOrderAdmission().quantity(snapshot, B, 'buy', Decimal(300),
                                                          order_price=Decimal(10)), Decimal(0))

    def test_target_validation_rejects_a_selected_candidate_that_became_stale(self):
        plan = self.planner.plan(self.provider.capture_planning())
        del self.host._market_status[B]
        self.assertFalse(ExposureValidator().validate(self.provider.capture_planning(), plan))
