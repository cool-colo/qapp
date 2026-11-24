from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from trading_orchestration.contexts.exposure_context import InstrumentSnapshot


def round_down(quantity: Decimal, increment: Decimal) -> Decimal:
    return (quantity / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def liquidation_quantity(item: InstrumentSnapshot, target: Decimal,
                         pending: Decimal = Decimal(0)) -> Decimal:
    """Only a zero target with its entire uncommitted balance sellable may use odd lots.

    Snapshot sellable quantity already excludes outstanding sells. Do not subtract
    them twice; pending also identifies the final slice of a full liquidation.
    """
    remaining = item.quantity - pending
    if item.allow_odd_lot_sell and target == 0 and 0 < remaining == item.sellable:
        return remaining
    return Decimal(0)


def corrective_quantity(item: InstrumentSnapshot, direction: str, *, needed: Decimal,
                        room: Decimal, cap: Decimal, order_price: Decimal,
                        liquidation: Decimal = Decimal(0)) -> Decimal:
    """Choose the legal quantity closest to the center within the caller's bounds.

    Planning supplies the mark as order_price; physical admission supplies the
    actual limit price to enforce the minimum buy notional only at execution.
    """
    if needed <= 0:
        return Decimal(0)
    ceiling = min(cap, room / item.price)
    minimum = item.minimum
    if direction == "buy":
        if not order_price.is_finite() or order_price <= 0:
            return Decimal(0)
        # The executor requires notional strictly greater than the configured minimum.
        minimum = max(minimum, round_down(item.minimum_buy_notional / order_price,
                                          item.increment) + item.increment)
    candidates = {Decimal(0)}
    largest = round_down(ceiling, item.increment)
    if largest >= minimum:
        lower = round_down(needed / item.price, item.increment)
        for quantity in (lower, lower + item.increment):
            candidates.add(min(largest, max(minimum, quantity)))
    if direction == "sell" and 0 < liquidation <= ceiling:
        candidates.add(liquidation)
    return min(candidates, key=lambda quantity: (abs(needed - quantity * item.price), quantity))
