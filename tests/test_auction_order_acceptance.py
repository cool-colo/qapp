from contextlib import nullcontext
from unittest.mock import MagicMock
import unittest

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderAccepted, OrderCanceled
from nautilus_trader.model.identifiers import AccountId, InstrumentId, VenueOrderId

from task_orchestration.models import Decision, RunState
from trading_orchestration.tasks.auction_execution_task import AuctionExecutionTask
from tests.test_opening_auction import replay_fixture, timestamp

A, B = "000001.SZ.QMT", "000002.SZ.QMT"


class AuctionOrderAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, _, _ = replay_fixture()
        self.strategy.clock.set_time(timestamp("2026-07-08 09:24:58"))
        self.strategy._desired_quantities = MagicMock(return_value={A: 1000})
        self.strategy._current_quantity = MagicMock(return_value=0)

    def tearDown(self):
        self.engine.dispose()

    def order(self, iid=A, *, tags=None, quantity=100):
        instrument = self.engine.cache.instrument(InstrumentId.from_str(iid))
        order = self.strategy.order_factory.limit(
            instrument_id=instrument.id, order_side=OrderSide.BUY,
            quantity=instrument.make_qty(quantity), price=instrument.make_price(10),
            tags=["opening_auction"] if tags is None else tags,
        )
        self.engine.cache.add_order(order)
        return order

    def accept(self, order):
        order.apply(OrderAccepted(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=order.instrument_id, client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(str(order.client_order_id)), account_id=AccountId("QMT-001"),
            event_id=UUID4(), ts_event=self.strategy.clock.timestamp_ns(),
            ts_init=self.strategy.clock.timestamp_ns(),
        ))
        self.engine.cache.update_order(order)

    def test_submission_waits_for_acceptance_and_accepts_partial_allocation(self):
        self.assertFalse(self.strategy.opening_auction_orders_accepted())
        order = self.order(quantity=100)
        self.assertFalse(self.strategy.opening_auction_orders_accepted())
        self.accept(order)
        self.assertTrue(self.strategy.opening_auction_orders_accepted())
        self.strategy._current_quantity.assert_called_with(InstrumentId.from_str(A))

    def test_every_member_and_every_slice_must_be_accepted(self):
        self.strategy._desired_quantities.return_value = {A: 1000, B: 1000}
        self.accept(self.order())
        self.assertFalse(self.strategy.opening_auction_orders_accepted())
        self.accept(self.order(B))
        pending_slice = self.order(B)
        self.assertFalse(self.strategy.opening_auction_orders_accepted())
        self.accept(pending_slice)
        self.assertTrue(self.strategy.opening_auction_orders_accepted())

    def test_ordinary_and_previous_day_orders_do_not_satisfy_gate(self):
        self.accept(self.order(tags=["ordinary"]))
        self.strategy.clock.set_time(timestamp("2026-07-07 09:24:58"))
        self.accept(self.order())
        self.strategy.clock.set_time(timestamp("2026-07-08 09:24:58"))
        self.assertFalse(self.strategy.opening_auction_orders_accepted())

    def test_canceled_accepted_order_does_not_satisfy_gate(self):
        order = self.order()
        self.accept(order)
        order.apply(OrderCanceled(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=order.instrument_id, client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id, account_id=order.account_id,
            event_id=UUID4(), ts_event=self.strategy.clock.timestamp_ns(),
            ts_init=self.strategy.clock.timestamp_ns(),
        ))
        self.engine.cache.update_order(order)
        self.assertFalse(self.strategy.opening_auction_orders_accepted())

    def test_no_orders_needed_completes_without_submission(self):
        self.strategy._current_quantity.return_value = 1000
        self.assertTrue(self.strategy.opening_auction_orders_accepted())

    def test_task_completes_on_acceptance_without_position_check(self):
        bridge = MagicMock()
        bridge.operations.side_effect = nullcontext
        bridge.host._within_auction_submit_time.return_value = True
        task = AuctionExecutionTask(bridge)
        task.preparation.prepare = MagicMock(return_value=Decision(RunState.SUCCEEDED, "ready"))
        bridge.host.opening_auction_orders_accepted.return_value = False
        self.assertEqual(task.advance(MagicMock()).state, RunState.RUNNING)
        bridge.host.opening_auction_orders_accepted.return_value = True
        result = task.advance(MagicMock())
        self.assertEqual((result.state, result.reason), (RunState.SUCCEEDED, "auction_orders_accepted"))
        bridge.target_satisfied.assert_not_called()
