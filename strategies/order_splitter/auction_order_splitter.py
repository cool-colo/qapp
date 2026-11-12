from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from decimal import ROUND_CEILING
from typing import Any

from nautilus_trader.model.enums import OrderSide

from strategies.order_splitter.notional_order_splitter import LimitOrderQuantityRules
from strategies.order_splitter.notional_order_splitter import NotionalOrderSplitter


@dataclass(frozen=True)
class AuctionAllocation:
    quantities: tuple[Decimal, ...] = ()
    notional: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    reason: str = ""

    @property
    def budget_used(self) -> Decimal:
        return self.notional + self.commission


class AuctionOrderSplitter:
    """Prefer whole targets; only allocate a large enough affordable partial order.

    The continuous-trading notional cap does not apply. Exchange quantity limits
    still apply, with minimum commission reserved for every physical order.
    """

    def __init__(self, commission_rate: Decimal, minimum_fee: Decimal,
                 partial_min_notional: Decimal) -> None:
        self.commission_rate = Decimal(str(commission_rate))
        self.minimum_fee = Decimal(str(minimum_fee))
        self.partial_min_notional = Decimal(str(partial_min_notional))
        for name, value in (("commission_rate", self.commission_rate),
                            ("minimum_fee", self.minimum_fee),
                            ("partial_min_notional", self.partial_min_notional)):
            if not value.is_finite() or value < 0:
                raise ValueError(f"Auction {name} must be finite and non-negative")
        if self.commission_rate >= 1:
            raise ValueError("Auction commission_rate must be less than 1")
        self._quantity_splitter = NotionalOrderSplitter(Decimal("0"))

    @staticmethod
    def ranked_ids(ranks: dict[str, int | None], side: OrderSide) -> list[str]:
        if side == OrderSide.BUY:
            return sorted((key for key, rank in ranks.items() if rank is not None),
                          key=lambda key: (ranks[key], key))
        if side == OrderSide.SELL:
            # Holdings absent from today's ranking are sold before ranked stocks.
            return sorted(ranks, key=lambda key: (ranks[key] is not None,
                                                 -(ranks[key] or 0), key))
        raise ValueError("Auction allocation requires BUY or SELL")

    def commission(self, notional: Decimal) -> Decimal:
        if notional <= 0:
            return Decimal("0")
        return max(self.minimum_fee, notional * self.commission_rate).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING)

    def _allocation(self, instrument: Any, quantity: Decimal, price: Decimal,
                    *, allow_odd_lot: bool = False, reason: str = "") -> AuctionAllocation:
        quantities = tuple(self._quantity_splitter.split(instrument, quantity, price,
                                                        allow_odd_lot=allow_odd_lot))
        return AuctionAllocation(quantities, sum(quantities, Decimal("0")) * price,
                                 sum((self.commission(q * price) for q in quantities), Decimal("0")),
                                 reason)

    def allocate(self, instrument: Any, quantity: Decimal, price: Decimal, budget: Decimal,
                 *, allow_odd_lot: bool = False) -> AuctionAllocation:
        quantity, price, budget = (Decimal(str(value)) for value in (quantity, price, budget))
        if any(not value.is_finite() for value in (quantity, price, budget)) or price <= 0:
            raise ValueError("Auction allocation requires finite values and a positive price")
        if quantity <= 0 or budget <= 0:
            return AuctionAllocation(reason="no_quantity_or_budget")
        full = self._allocation(instrument, quantity, price, allow_odd_lot=allow_odd_lot,
                                reason="whole_target")
        if sum(full.quantities, Decimal("0")) != quantity:
            return AuctionAllocation(reason="untradeable_target_quantity")
        if full.budget_used <= budget:
            return full

        # Partial liquidation cannot use odd lots: the remainder still exists.
        # Find the maximum legal quantity including commission on every slice.
        rules = LimitOrderQuantityRules.from_instrument(instrument)
        lower = 0
        upper = int(rules.round_down(min(quantity, budget // price)) // rules.quantity_increment)
        best = AuctionAllocation(reason="insufficient_budget")
        while lower <= upper:
            middle = (lower + upper) // 2
            trial = self._allocation(instrument, rules.quantity_increment * middle, price,
                                     reason="partial_target")
            if trial.budget_used <= budget:
                if trial.notional > best.notional:
                    best = trial
                lower = middle + 1
            else:
                upper = middle - 1
        if best.notional <= self.partial_min_notional:
            return AuctionAllocation(reason="partial_below_min_notional")
        if (quantity - sum(best.quantities, Decimal("0"))) * price < self.partial_min_notional:
            return AuctionAllocation(reason="remaining_target_below_min_notional")
        return best
