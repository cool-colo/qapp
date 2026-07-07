from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any


class NotionalOrderSplitter:
    """Splits a target delta into same-side orders capped by notional amount."""

    def __init__(self, max_order_notional: Decimal) -> None:
        self.max_order_notional = Decimal(str(max_order_notional))

    def split(self, instrument: Any, quantity: Decimal, price: Decimal) -> list[Decimal]:
        if quantity <= 0:
            return []
        if self.max_order_notional <= 0 or price <= 0:
            return [quantity]
        if quantity * price <= self.max_order_notional:
            return [quantity]

        lot_size = Decimal(str(getattr(instrument, "lot_size", Decimal("1"))))
        if lot_size <= 0:
            lot_size = Decimal("1")
        max_qty = (self.max_order_notional / price // lot_size) * lot_size
        if max_qty <= 0:
            return [quantity]

        slices: list[Decimal] = []
        remaining = quantity
        while remaining > max_qty:
            slices.append(max_qty)
            remaining -= max_qty
        if remaining > 0:
            slices.append(remaining)
        return slices