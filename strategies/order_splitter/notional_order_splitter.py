from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from typing import Any


@dataclass(frozen=True)
class LimitOrderQuantityRules:
    """Normalized instrument reference data for regular and residual limit orders."""

    min_quantity: Decimal
    max_quantity: Decimal
    quantity_increment: Decimal
    allow_odd_lot_sell: bool

    @classmethod
    def from_instrument(cls, instrument: Any) -> LimitOrderQuantityRules:
        raw = instrument.info["limit_order_quantity_rules"]
        if not isinstance(raw, dict):
            raise ValueError(f"Missing limit-order quantity rules for {instrument.id}")
        rules = cls(
            min_quantity=Decimal(str(raw["min_quantity"])),
            max_quantity=Decimal(str(raw["max_quantity"])),
            quantity_increment=Decimal(str(raw["quantity_increment"])),
            allow_odd_lot_sell=raw["allow_odd_lot_sell"],
        )
        values = (rules.min_quantity, rules.max_quantity, rules.quantity_increment)
        if (
            any(not value.is_finite() or value <= 0 for value in values)
            or rules.min_quantity > rules.max_quantity
            or rules.min_quantity % rules.quantity_increment
            or rules.max_quantity % rules.quantity_increment
            or not isinstance(rules.allow_odd_lot_sell, bool)
        ):
            raise ValueError(f"Invalid limit-order quantity rules for {instrument.id}: {raw}")
        return rules

    def round_down(self, quantity: Decimal) -> Decimal:
        return (quantity // self.quantity_increment) * self.quantity_increment

    def is_valid(self, quantity: Decimal, *, allow_odd_lot: bool = False) -> bool:
        if (
            not quantity.is_finite()
            or quantity <= 0
            or quantity != quantity.to_integral_value()
            or quantity > self.max_quantity
        ):
            return False
        if allow_odd_lot and self.allow_odd_lot_sell:
            return True
        return quantity >= self.min_quantity and quantity % self.quantity_increment == 0


class NotionalOrderSplitter:
    """Split within instrument limits and the configured per-order notional cap.

    Untradeable buy/partial-sell remainders stay pending rather than increasing
    the requested quantity. Only liquidation may retain an odd-sized last slice;
    the executor must verify it consumes the remaining sellable balance.
    """

    def __init__(self, max_order_notional: Decimal) -> None:
        self.max_order_notional = Decimal(str(max_order_notional))

    def split(
        self,
        instrument: Any,
        quantity: Decimal,
        price: Decimal,
        *,
        allow_odd_lot: bool = False,
    ) -> list[Decimal]:
        if quantity <= 0:
            return []
        if not quantity.is_finite() or quantity != quantity.to_integral_value():
            raise ValueError(f"Order quantity must be whole shares: {quantity}")
        rules = LimitOrderQuantityRules.from_instrument(instrument)
        allow_odd_lot = allow_odd_lot and rules.allow_odd_lot_sell
        maximum = rules.max_quantity
        if self.max_order_notional > 0:
            if not price.is_finite() or price <= 0:
                raise ValueError(f"Order price must be positive: {price}")
            maximum = min(
                maximum,
                (self.max_order_notional / price).to_integral_value(rounding=ROUND_FLOOR),
            )
        if quantity <= maximum and rules.is_valid(quantity, allow_odd_lot=allow_odd_lot):
            return [quantity]

        maximum = rules.round_down(maximum)
        if maximum < rules.min_quantity:
            return []
        if not allow_odd_lot:
            quantity = rules.round_down(quantity)
        if quantity < rules.min_quantity:
            return []

        slices = [maximum] * int(quantity // maximum)
        remaining = quantity % maximum
        if remaining == 0:
            return slices

        if remaining < rules.min_quantity:
            needed = (
                (rules.min_quantity - remaining) / rules.quantity_increment
            ).to_integral_value(rounding=ROUND_CEILING) * rules.quantity_increment
            available = sum(
                (rules.round_down(value - rules.min_quantity) for value in slices),
                Decimal("0"),
            )
            if available >= needed:
                for index in range(len(slices) - 1, -1, -1):
                    moved = min(needed, rules.round_down(slices[index] - rules.min_quantity))
                    slices[index] -= moved
                    remaining += moved
                    needed -= moved
                    if needed == 0:
                        break

        if rules.is_valid(remaining, allow_odd_lot=allow_odd_lot):
            slices.append(remaining)
        return slices
