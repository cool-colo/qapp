from __future__ import annotations

from decimal import Decimal
from datetime import time
from typing import Callable

import pandas as pd

from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Money

from backtests.data_providers.historical_ticks import HistoricalTick
from backtests.data_providers.historical_ticks import continuous_session_index


class AuctionReplayFillModel(FillModel):
    """No pre-open fills; shared, bounded liquidity per historical observation."""

    def __init__(self, now_ns: Callable[[], int], participation_rate: float = 0.01):
        super().__init__()
        if not 0 < participation_rate <= 1:
            raise ValueError("auction participation rate must be in (0, 1]")
        self.now_ns = now_ns
        self.participation_rate = participation_rate
        self.auction_results: dict[tuple[str, object], float] = {}
        self.closing_auction_results: dict[tuple[str, object], float] = {}
        self.final_observations: dict[tuple[str, object, str], HistoricalTick] = {}
        self.latest: dict[str, HistoricalTick] = {}
        self.consumed: dict[tuple[str, int, OrderSide], int] = {}

    def observe(self, instrument_id: str, row: HistoricalTick) -> None:
        if row.kind in {"auction", "closing_auction"}:
            day = pd.Timestamp(row.ts_event, unit="ns", tz="UTC").tz_convert("Asia/Shanghai").date()
            key = (instrument_id, day, row.kind)
            existing = self.final_observations.get(key)
            if existing is not None and existing != row:
                raise ValueError(f"Multiple final {row.kind} results for {instrument_id} on {day}")
            # The same observation is delivered through both depth and QuoteTick.
            self.final_observations[key] = row
            results = self.auction_results if row.kind == "auction" else self.closing_auction_results
            results[(instrument_id, day)] = row.bid_price
        previous = self.latest.get(instrument_id)
        if previous is None or row.ts_event > previous.ts_event:
            self.latest[instrument_id] = row

    def get_orderbook_for_fill_simulation(self, instrument, order, best_bid, best_ask):
        book = OrderBook(instrument.id, BookType.L2_MBP)
        row = self.latest.get(str(instrument.id))
        now = self.now_ns()
        if row is None or row.ts_init > now or row.kind in {"indicative", "closing_indicative"}:
            return book
        local_time = pd.Timestamp(now, unit="ns", tz="UTC").tz_convert("Asia/Shanghai")
        event_time = pd.Timestamp(row.ts_event, unit="ns", tz="UTC").tz_convert("Asia/Shanghai")
        if local_time.date() != event_time.date():
            return book
        liquidity_ts = row.ts_event
        if row.kind in {"auction", "closing_auction"}:
            clearing_time = time(9, 25) if row.kind == "auction" else time(15)
            clearing_ts = (event_time.normalize() + pd.Timedelta(
                hours=clearing_time.hour, minutes=clearing_time.minute)).value
            if now < clearing_ts or (row.kind == "auction" and local_time.time() >= time(9, 30)):
                return book
            # Historical receive delay must not let late venue arrivals join an
            # auction which already cleared at exchange time.
            if order.ts_accepted >= clearing_ts:
                return book
            size = int(row.bid_size * self.participation_rate)
            price = Decimal(str(row.bid_price))
            liquidity_ts = clearing_ts
        elif row.kind == "quote":
            session = continuous_session_index(event_time.time())
            if session is None or continuous_session_index(local_time.time()) != session:
                return book
            # Execute only against a newly received observation. Between ticks,
            # or when one-sided quotes were excluded, old displayed liquidity
            # is not evidence that a new order can still trade at that price.
            if now != row.ts_init:
                return book
            size = row.ask_size if order.side == OrderSide.BUY else row.bid_size
            price = Decimal(str(row.ask_price if order.side == OrderSide.BUY else row.bid_price))
        else:
            raise ValueError(f"Unknown historical tick kind: {row.kind}")
        limit = order.price.as_decimal()
        if (order.side == OrderSide.BUY and limit < price
                or order.side == OrderSide.SELL and limit > price):
            return book
        key = (str(instrument.id), liquidity_ts, order.side)
        available = size - self.consumed.get(key, 0)
        quantity = min(available, int(order.leaves_qty.as_decimal()))
        if quantity <= 0:
            return book
        self.consumed[key] = self.consumed.get(key, 0) + quantity
        liquidity_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
        book.add(BookOrder(liquidity_side, instrument.make_price(price),
                           instrument.make_qty(quantity), 1), ts_event=row.ts_event)
        return book


class AuctionReplayModule(SimulationModule):
    def __init__(self, fill_model: AuctionReplayFillModel,
                 rows: dict[tuple[str, int], HistoricalTick]):
        super().__init__(SimulationModuleConfig())
        self.fill_model = fill_model
        self.rows = rows

    def pre_process(self, data):
        key = (str(data.instrument_id), int(data.ts_init))
        if key in self.rows:
            self.fill_model.observe(key[0], self.rows[key])

    def process(self, ts_now):
        return

    def log_diagnostics(self, logger):
        return

    def reset(self):
        self.fill_model.auction_results.clear()
        self.fill_model.closing_auction_results.clear()
        self.fill_model.final_observations.clear()
        self.fill_model.latest.clear()
        self.fill_model.consumed.clear()


class AShareReplayFeeModel(FeeModel):
    """Explicit brokerage and sell-side stamp duty assumptions for comparisons."""

    def __init__(self, commission_rate: float, minimum_commission: float,
                 stamp_duty_rate: float):
        super().__init__()
        values = (commission_rate, minimum_commission, stamp_duty_rate)
        if any(not Decimal(str(value)).is_finite() or value < 0 for value in values):
            raise ValueError("Replay fees must be finite and non-negative")
        self.commission_rate = Decimal(str(commission_rate))
        self.minimum_commission = Decimal(str(minimum_commission))
        self.stamp_duty_rate = Decimal(str(stamp_duty_rate))
        self.charged: dict[str, Decimal] = {}

    def get_commission(self, order, fill_qty, fill_px, instrument):
        notional = fill_qty.as_decimal() * fill_px.as_decimal()
        key = str(order.client_order_id)
        previous = self.charged.get(key, Decimal("0"))
        accumulated = previous + notional * self.commission_rate
        brokerage = max(accumulated, self.minimum_commission) - (
            max(previous, self.minimum_commission) if key in self.charged else Decimal("0"))
        self.charged[key] = accumulated
        stamp = notional * self.stamp_duty_rate if order.side == OrderSide.SELL else Decimal("0")
        return Money(brokerage + stamp, instrument.quote_currency)


def auction_execution_report(engine, strategy, model) -> pd.DataFrame:
    """Per-symbol auction outcome; final-volume coverage is reported explicitly."""
    fills = engine.trader.generate_fills_report()
    rows = []
    for order in engine.cache.orders():
        if "opening_auction" not in (order.tags or []):
            continue
        submitted = pd.Timestamp(order.ts_init, unit="ns", tz="UTC").tz_convert("Asia/Shanghai")
        auction_fills = []
        if not fills.empty:
            selected = fills[fills.index.astype(str) == str(order.client_order_id)]
            for _, fill in selected.iterrows():
                event = pd.Timestamp(fill.ts_event).tz_convert("Asia/Shanghai")
                if event.date() == submitted.date() and "09:25:00" <= event.strftime("%H:%M:%S") < "09:30:00":
                    auction_fills.append(fill)
        filled = sum(int(fill.last_qty) for fill in auction_fills)
        amount = sum(float(fill.last_qty) * float(fill.last_px) for fill in auction_fills)
        requested = int(order.quantity.as_decimal())
        instrument_id = str(order.instrument_id)
        open_price = model.auction_results.get((instrument_id, submitted.date()))
        average = amount / filled if filled else None
        sign = 1 if order.side == OrderSide.BUY else -1
        rows.append(dict(
            trade_date=submitted.date(), instrument_id=instrument_id, side=order.side.name,
            submitted_quantity=requested, limit_price=float(order.price),
            reserved_notional=requested * float(order.price), auction_filled_quantity=filled,
            auction_filled_amount=amount, auction_fill_rate=filled / requested,
            auction_remaining_quantity=requested - filled,
            final_open_price=open_price, final_volume_available=open_price is not None,
            auction_average_price=average,
            execution_cost_bps=(sign * (average / open_price - 1) * 10000
                                if average is not None and open_price else None),
            participation_rate=model.participation_rate,
        ))
    return pd.DataFrame(rows)
