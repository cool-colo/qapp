from __future__ import annotations

from strategies.pricing.base import BuyPriceStrategy
from strategies.pricing.base import SellPriceStrategy
from strategies.pricing.base import base_offset
from strategies.pricing.base import walk_book
from strategies.pricing.context import PriceContext


class OpenOffsetSellPriceStrategy(SellPriceStrategy):
    """
    Sell price policy anchored to today's open:

        price = open - max(open * offset_bps/10000, tick)

    While ``cancel_count < cancel_threshold`` the base-offset price is used.
    Once the order has been cancelled ``cancel_threshold`` times or more, the
    price walks *down the bid ladder* to cross toward buyers and fill quickly,
    stepping one extra tick deeper per additional cancel. There is no hard floor:
    the exchange down-limit is handled by the executor's symbol-freeze logic.
    """

    def __init__(self, offset_bps: float = 5.0, cancel_threshold: int = 1) -> None:
        self.offset_bps = float(offset_bps)
        self.cancel_threshold = int(cancel_threshold)

    def _compute_sell(self, ctx: PriceContext) -> float | None:
        base = ctx.base_price()
        if base is None:
            return None
        offset = base_offset(base, ctx.tick, self.offset_bps)
        base_price = base - offset

        if ctx.cancel_count < self.cancel_threshold:
            return _positive(base_price)

        book_price = walk_book(ctx.bids, ctx.quantity)
        if book_price is None:
            book_price = ctx.best_bid
        if book_price is None or book_price <= 0:
            return _positive(base_price)

        # Escalate: one extra tick below the covering bid level per cancel past
        # the threshold, to keep crossing until we fill.
        extra_steps = ctx.cancel_count - self.cancel_threshold + 1
        aggressive = book_price - extra_steps * ctx.tick
        # Never price *above* the base-offset price (that would be less aggressive).
        return _positive(min(base_price, aggressive))


class OpenOffsetBuyPriceStrategy(BuyPriceStrategy):
    """
    Buy price policy anchored to today's open:

        price = open + max(open * offset_bps/10000, tick)

    While ``cancel_count < cancel_threshold`` the base-offset price is used.
    Once cancelled ``cancel_threshold`` times or more, the price walks *up the
    ask ladder* to cross toward sellers, stepping one extra tick higher per
    additional cancel, but is always capped at::

        max_buy_price = open + max(open * max_price_bps/10000, tick)

    Callers must not cancel an order already resting at this cap; it is left to
    fill if the market comes back down.
    """

    def __init__(
        self,
        offset_bps: float = 5.0,
        max_price_bps: float = 10.0,
        cancel_threshold: int = 2,
    ) -> None:
        self.offset_bps = float(offset_bps)
        self.max_price_bps = float(max_price_bps)
        self.cancel_threshold = int(cancel_threshold)

    def max_buy_price(self, ctx: PriceContext) -> float | None:
        base = ctx.base_price()
        if base is None:
            return None
        return base + base_offset(base, ctx.tick, self.max_price_bps)

    def _compute_buy(self, ctx: PriceContext) -> float | None:
        base = ctx.base_price()
        if base is None:
            return None
        offset = base_offset(base, ctx.tick, self.offset_bps)
        base_price = base + offset
        cap = base + base_offset(base, ctx.tick, self.max_price_bps)

        if ctx.cancel_count < self.cancel_threshold:
            return _positive(min(base_price, cap))

        book_price = walk_book(ctx.asks, ctx.quantity)
        if book_price is None:
            book_price = ctx.best_ask
        if book_price is None or book_price <= 0:
            return _positive(min(base_price, cap))

        # Escalate: one extra tick above the covering ask level per cancel past
        # the threshold.
        extra_steps = ctx.cancel_count - self.cancel_threshold + 1
        aggressive = book_price + extra_steps * ctx.tick
        # Never below the base-offset price, never above the max-buy cap.
        return _positive(min(max(base_price, aggressive), cap))


def _positive(price: float) -> float | None:
    return price if price > 0 else None
