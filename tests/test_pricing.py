from __future__ import annotations

import unittest
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from strategies.pricing import OpenOffsetBuyPriceStrategy
from strategies.pricing import OpenOffsetSellPriceStrategy
from strategies.pricing import PriceContext
from strategies.pricing import base_offset
from strategies.pricing import walk_book


INST = InstrumentId.from_str("000001.SZ.QMT")


def sell_ctx(**overrides) -> PriceContext:
    defaults = dict(
        instrument_id=INST,
        side=OrderSide.SELL,
        open_price=10.0,
        last_close=10.0,
        tick=0.01,
        quantity=Decimal("100"),
        cancel_count=0,
    )
    defaults.update(overrides)
    return PriceContext(**defaults)


def buy_ctx(**overrides) -> PriceContext:
    defaults = dict(
        instrument_id=INST,
        side=OrderSide.BUY,
        open_price=10.0,
        last_close=10.0,
        tick=0.01,
        quantity=Decimal("100"),
        cancel_count=0,
    )
    defaults.update(overrides)
    return PriceContext(**defaults)


class BaseOffsetTest(unittest.TestCase):
    def test_proportional_offset_wins_when_larger_than_tick(self) -> None:
        # 100 * 5bps = 0.05 > tick 0.01
        self.assertAlmostEqual(base_offset(100.0, 0.01, 5.0), 0.05)

    def test_tick_floor_wins_when_proportional_is_tiny(self) -> None:
        # 10 * 5bps = 0.005 < tick 0.01
        self.assertAlmostEqual(base_offset(10.0, 0.01, 5.0), 0.01)


class WalkBookTest(unittest.TestCase):
    def test_returns_none_for_empty_ladder(self) -> None:
        self.assertIsNone(walk_book([], Decimal("100")))

    def test_stops_at_level_that_covers_quantity(self) -> None:
        ladder = [(11.35, 21), (11.34, 13), (11.33, 53)]
        # need 30 -> 21 not enough, 21+13=34 covers at 11.34
        self.assertAlmostEqual(walk_book(ladder, Decimal("30")), 11.34)

    def test_reaches_deepest_level_when_size_insufficient(self) -> None:
        ladder = [(11.35, 21), (11.34, 13)]
        self.assertAlmostEqual(walk_book(ladder, Decimal("1000")), 11.34)


class SellPriceStrategyTest(unittest.TestCase):
    def test_base_offset_when_no_cancels(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        # open 10, offset max(10*5bps=0.005, tick 0.01)=0.01 -> 9.99
        self.assertAlmostEqual(pricer.compute(sell_ctx(cancel_count=0)), 9.99)

    def test_uses_open_over_last_close(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        price = pricer.compute(sell_ctx(open_price=20.0, last_close=10.0, cancel_count=0))
        # 20 - max(20*5bps=0.01, 0.01)=0.01 -> 19.99
        self.assertAlmostEqual(price, 19.99)

    def test_returns_none_when_open_missing(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        price = pricer.compute(sell_ctx(open_price=None, last_close=10.0, cancel_count=0))
        self.assertIsNone(price)

    def test_walks_bid_ladder_when_cancelled(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        bids = [(11.35, 21), (11.34, 13), (11.33, 53)]
        ctx = sell_ctx(
            open_price=11.40,
            quantity=Decimal("30"),
            cancel_count=1,
            best_bid=11.35,
            bids=bids,
        )
        # covering level 11.34, extra_steps=1 -> 11.34 - 0.01 = 11.33
        self.assertAlmostEqual(pricer.compute(ctx), 11.33)

    def test_escalates_deeper_with_more_cancels(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        bids = [(11.35, 100)]
        ctx = sell_ctx(
            open_price=11.40,
            quantity=Decimal("30"),
            cancel_count=3,
            best_bid=11.35,
            bids=bids,
        )
        # covering level 11.35, extra_steps = 3-1+1 = 3 -> 11.35 - 0.03 = 11.32
        self.assertAlmostEqual(pricer.compute(ctx), 11.32)

    def test_empty_book_falls_back_to_base_offset_even_when_cancelled(self) -> None:
        pricer = OpenOffsetSellPriceStrategy(offset_bps=5.0, cancel_threshold=1)
        ctx = sell_ctx(open_price=10.0, cancel_count=5, bids=[], best_bid=None)
        self.assertAlmostEqual(pricer.compute(ctx), 9.99)


class BuyPriceStrategyTest(unittest.TestCase):
    def test_base_offset_when_below_threshold(self) -> None:
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=5.0, max_price_bps=10.0, cancel_threshold=2)
        # open 10 + max(0.005, 0.01) = 10.01
        self.assertAlmostEqual(pricer.compute(buy_ctx(cancel_count=1)), 10.01)

    def test_walks_ask_ladder_when_cancelled(self) -> None:
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=5.0, max_price_bps=100.0, cancel_threshold=2)
        asks = [(11.36, 9), (11.37, 48), (11.38, 114)]
        ctx = buy_ctx(
            open_price=11.30,
            quantity=Decimal("30"),
            cancel_count=2,
            best_ask=11.36,
            asks=asks,
        )
        # covering level: 9 not enough, 9+48=57 covers at 11.37; extra_steps=1 -> 11.38
        self.assertAlmostEqual(pricer.compute(ctx), 11.38)

    def test_never_exceeds_max_buy_cap(self) -> None:
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=5.0, max_price_bps=10.0, cancel_threshold=2)
        asks = [(50.0, 1000)]
        ctx = buy_ctx(
            open_price=10.0,
            quantity=Decimal("30"),
            cancel_count=5,
            best_ask=50.0,
            asks=asks,
        )
        # cap = 10 + max(10*10bps=0.01, tick 0.01) = 10.01
        self.assertAlmostEqual(pricer.compute(ctx), 10.01)

    def test_max_buy_cap_uses_proportional_when_larger(self) -> None:
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=5.0, max_price_bps=10.0, cancel_threshold=2)
        # open 100 -> cap = 100 + max(100*10bps=0.1, 0.01) = 100.1
        ctx = buy_ctx(open_price=100.0, last_close=100.0, cancel_count=0)
        self.assertAlmostEqual(pricer.max_buy_price(ctx), 100.1)

    def test_empty_book_falls_back_to_base_offset(self) -> None:
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=5.0, max_price_bps=10.0, cancel_threshold=2)
        ctx = buy_ctx(open_price=10.0, cancel_count=5, asks=[], best_ask=None)
        self.assertAlmostEqual(pricer.compute(ctx), 10.01)

    def test_base_price_below_threshold_still_capped(self) -> None:
        # if base offset already exceeds the cap, the cap wins
        pricer = OpenOffsetBuyPriceStrategy(offset_bps=20.0, max_price_bps=10.0, cancel_threshold=2)
        ctx = buy_ctx(open_price=100.0, last_close=100.0, cancel_count=0)
        # base = 100 + 0.2 = 100.2, cap = 100 + 0.1 = 100.1 -> min -> 100.1
        self.assertAlmostEqual(pricer.compute(ctx), 100.1)


if __name__ == "__main__":
    unittest.main()
