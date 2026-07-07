from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId


@dataclass(frozen=True)
class PriceContext:
    """
    Immutable snapshot of everything a stateless price strategy needs to price a
    single order. Built by the executing strategy from its cache/book state and
    passed into ``PriceStrategy.compute``.

    The price strategy holds no state of its own; per-instrument, per-side cancel
    counts and today's open price are tracked by the executor and injected here.
    """

    instrument_id: InstrumentId
    side: OrderSide
    open_price: float | None
    last_close: float | None
    tick: float
    quantity: Decimal
    cancel_count: int
    best_bid: float | None = None
    best_ask: float | None = None
    # (price, size) levels, best-first. May be empty (e.g. backtest with no book).
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)

    def base_price(self) -> float | None:
        """Reference price for the base offset: today's open, else last close."""
        if self.open_price is not None and self.open_price > 0:
            return self.open_price
        if self.last_close is not None and self.last_close > 0:
            return self.last_close
        return None
