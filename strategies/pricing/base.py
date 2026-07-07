from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide

from strategies.pricing.context import PriceContext


def base_offset(base_price: float, tick: float, offset_bps: float) -> float:
    """
    The symmetric base offset used by both sides: ``max(base * bps/10000, tick)``.

    ``offset_bps`` is in basis points (e.g. 5.0 == 5/10000 == 0.05%).
    """
    proportional = base_price * (offset_bps / 10_000.0)
    return max(proportional, tick)


def walk_book(
    levels: list[tuple[float, float]],
    quantity: Decimal,
) -> float | None:
    """
    Walk a price/size ladder (best-first) accumulating size until it covers
    ``quantity``. Return the price of the level that clears the requested
    quantity (i.e. the deepest level we must reach to fill). Returns ``None``
    when the ladder is empty.

    Used to price aggressively enough to fill: for a sell we walk the bid ladder
    (cross toward buyers), for a buy we walk the ask ladder (cross toward sellers).
    """
    if not levels:
        return None
    target = float(quantity)
    accumulated = 0.0
    last_price: float | None = None
    for price, size in levels:
        if price <= 0 or size <= 0:
            continue
        last_price = price
        accumulated += size
        if accumulated >= target:
            return price
    # Not enough resting size to fully cover the quantity: reach as deep as the
    # book goes (the last usable level).
    return last_price


class PriceStrategy(ABC):
    """
    Stateless limit-price policy. Given an immutable :class:`PriceContext`,
    returns a raw float price (or ``None`` to signal "no price / use market").
    The caller quantizes via ``instrument.make_price``.

    Implementations MUST NOT retain state across calls.
    """

    @abstractmethod
    def compute(self, ctx: PriceContext) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError


class BuyPriceStrategy(PriceStrategy):
    """Price policy for BUY orders. Walks the ask ladder when escalating."""

    def compute(self, ctx: PriceContext) -> float | None:
        assert ctx.side == OrderSide.BUY, "BuyPriceStrategy received a non-BUY context"
        return self._compute_buy(ctx)

    @abstractmethod
    def _compute_buy(self, ctx: PriceContext) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError


class SellPriceStrategy(PriceStrategy):
    """Price policy for SELL orders. Walks the bid ladder when escalating."""

    def compute(self, ctx: PriceContext) -> float | None:
        assert ctx.side == OrderSide.SELL, "SellPriceStrategy received a non-SELL context"
        return self._compute_sell(ctx)

    @abstractmethod
    def _compute_sell(self, ctx: PriceContext) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError
