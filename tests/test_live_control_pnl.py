from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import LiquiditySide, OrderSide
from nautilus_trader.model.events import OrderAccepted, OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId, ClientOrderId, InstrumentId, StrategyId, TradeId, TraderId, VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import LimitOrder

from lives.control_server import LiveControlServer


DAY = date(2026, 11, 19)
IID = InstrumentId.from_str("600824.SH.BIGQMT")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def filled_order(order_id, quantities, price, *, reconciliation=False, tags=None,
                 day=DAY, side=OrderSide.SELL):
    timestamp = int(datetime.combine(day, datetime.min.time(), SHANGHAI).timestamp()) * 1_000_000_000
    order = LimitOrder(
        trader_id=TraderId("TEST-001"), strategy_id=StrategyId("MODEL-001"),
        instrument_id=IID, client_order_id=ClientOrderId(order_id),
        order_side=side, quantity=Quantity.from_int(sum(quantities)),
        price=Price.from_str(price), init_id=UUID4(), ts_init=timestamp, tags=tags,
    )
    order.apply(OrderAccepted(
        trader_id=order.trader_id, strategy_id=order.strategy_id,
        instrument_id=IID, client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId(order_id), account_id=AccountId("BIGQMT-001"),
        event_id=UUID4(), ts_event=timestamp, ts_init=timestamp,
    ))
    for index, quantity in enumerate(quantities):
        order.apply(OrderFilled(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=IID, client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id, account_id=AccountId("BIGQMT-001"),
            trade_id=TradeId(f"{order_id}-{index}"), position_id=None,
            order_side=side, order_type=order.order_type,
            last_qty=Quantity.from_int(quantity), last_px=Price.from_str(price),
            currency=CNY, commission=Money(0, CNY),
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
            event_id=UUID4(), ts_event=timestamp, ts_init=timestamp,
            reconciliation=reconciliation,
        ))
    return order


class FakeCache:
    def __init__(self, orders):
        self._orders = orders

    def orders(self, instrument_id, side):
        return [order for order in self._orders
                if order.instrument_id == instrument_id and order.side == side]

    def orders_open(self, instrument_id, side):
        return [order for order in self.orders(instrument_id, side) if order.is_open]

    def instrument(self, instrument_id):
        return SimpleNamespace(info={"name": "益民集团"})


class FakeStrategy:
    def __init__(self):
        self._stock_by_instrument = {str(IID): "600824.SH"}
        self._market_status = {
            str(IID): SimpleNamespace(last_price=3.41, last_close=3.52),
        }
        self._last_close = {str(IID): 3.52}

    def control_clock_date(self):
        return DAY

    def control_event_date(self, timestamp):
        return datetime.fromtimestamp(timestamp / 1_000_000_000, SHANGHAI).date()

    def venue_sellable_quantity(self, instrument_id):
        return Decimal(10600)


class RealtimePositionPnlTests(unittest.TestCase):
    def server(self, orders):
        return LiveControlServer(
            node=SimpleNamespace(cache=FakeCache(orders)),
            strategy_ref=FakeStrategy(), account_id="A1", trader_id="T1",
        )

    def test_partial_auction_sell_does_not_gain_from_synthetic_reconciliation(self):
        server = self.server([
            filled_order("actual", [10800, 6700], "3.50"),
            filled_order("synthetic", [28100], "3.78", reconciliation=True),
        ])
        self.assertEqual(server._today_sells(str(IID)), {
            "sell_qty": 17500.0, "sell_notional": 61250.0,
        })
        position = SimpleNamespace(
            instrument_id=IID, quantity=Quantity.from_int(10600),
            avg_px_open=3.8070025839793282,
        )
        row = server._open_row(position, str(IID))
        # Real sells: (3.50-3.52)*17500=-350; held shares: (3.41-3.52)*10600=-1166.
        self.assertAlmostEqual(row["day_pnl"], -1516.0)
        self.assertAlmostEqual(row["day_change"], -0.03125)
        self.assertAlmostEqual(row["unrealized_pnl"], -4208.227390180878)

    def test_closed_position_uses_actual_sells_instead_of_reconciled_average(self):
        server = self.server([
            filled_order("auction", [416, 384], "25.29"),
            filled_order("afternoon", [200], "24.18"),
            filled_order("synthetic", [200], "27.39", reconciliation=True),
            filled_order("previous", [200], "30.00", day=DAY - timedelta(days=1)),
        ])
        server._strategy._market_status[str(IID)] = SimpleNamespace(
            last_price=23.78, last_close=25.8,
        )
        position = SimpleNamespace(
            avg_px_open=27.390555555555554, avg_px_close=26.496,
            peak_qty=Quantity.from_int(1200),
        )
        row = server._closed_row(position, str(IID))
        self.assertAlmostEqual(row["day_pnl"], -732.0)
        self.assertEqual(row["last_price"], 23.78)
        self.assertEqual(row["volume"], 0)

    def test_closed_position_without_actual_fills_leaves_day_pnl_unknown(self):
        server = self.server([
            filled_order("synthetic", [1200], "27.39", reconciliation=True),
        ])
        position = SimpleNamespace(
            avg_px_open=27.39, avg_px_close=26.496,
            peak_qty=Quantity.from_int(1200),
        )
        self.assertIsNone(server._closed_row(position, str(IID))["day_pnl"])

    def test_reconciliation_order_tags_previous_day_and_buys_are_excluded(self):
        server = self.server([
            filled_order("actual", [100], "3.50"),
            filled_order("tagged", [28100], "3.78", tags=["RECONCILIATION"]),
            filled_order("previous-day", [100], "5.00", day=DAY - timedelta(days=1)),
            filled_order("buy", [100], "3.50", side=OrderSide.BUY),
        ])
        self.assertEqual(server._today_sells(str(IID)), {
            "sell_qty": 100.0, "sell_notional": 350.0,
        })


if __name__ == "__main__":
    unittest.main()
