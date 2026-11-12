from __future__ import annotations

from strategies.pricing.base import BuyPriceStrategy
from strategies.pricing.base import PriceStrategyLogger
from strategies.pricing.base import SellPriceStrategy
from strategies.pricing.context import PriceContext


class SameSideBestBuyPriceStrategy(BuyPriceStrategy):
    """Use best bid below the threshold; otherwise improve it by one tick."""

    def __init__(
        self,
        *,
        price_improvement_threshold: float,
        logger: PriceStrategyLogger,
    ) -> None:
        super().__init__(logger=logger)
        self.price_improvement_threshold = float(price_improvement_threshold)

    def _compute_buy(self, ctx: PriceContext) -> float | None:
        if ctx.best_bid is None or ctx.best_bid <= 0:
            self.logger.warning("No best bid available for buy price computation")
            return None
        price = ctx.best_bid
        if ctx.best_bid >= self.price_improvement_threshold:
            price += ctx.tick
        return price if price > 0 else None


class SameSideBestSellPriceStrategy(SellPriceStrategy):
    """Use best ask below the threshold; otherwise improve it by one tick."""

    def __init__(
        self,
        *,
        price_improvement_threshold: float,
        logger: PriceStrategyLogger,
    ) -> None:
        super().__init__(logger=logger)
        self.price_improvement_threshold = float(price_improvement_threshold)

    def _compute_sell(self, ctx: PriceContext) -> float | None:
        if ctx.best_ask is None or ctx.best_ask <= 0:
            self.logger.warning("No best ask available for sell price computation")
            return None
        price = ctx.best_ask
        if ctx.best_ask >= self.price_improvement_threshold:
            price -= ctx.tick
        return price if price > 0 else None
