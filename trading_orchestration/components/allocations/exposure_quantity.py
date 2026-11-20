from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

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
                        room: Decimal, cap: Decimal,
                        liquidation: Decimal = Decimal(0)) -> Decimal:
    """Choose a legal corrective quantity without crossing any caller's upper bound."""
    ceiling = min(cap, room / item.price)
    required = (needed / item.price / item.increment).to_integral_value(
        rounding=ROUND_CEILING) * item.increment
    minimum = item.minimum
    if direction == "buy":
        if item.buy_price is None or not item.buy_price.is_finite() or item.buy_price <= 0:
            return Decimal(0)
        # The executor requires notional strictly greater than the configured minimum.
        minimum = max(minimum, round_down(item.minimum_buy_notional / item.buy_price,
                                          item.increment) + item.increment)
    regular = min(round_down(ceiling, item.increment), max(minimum, required))
    if regular < minimum:
        regular = Decimal(0)
    # Full liquidation may be below the regular minimum or include a residual lot.
    # Prefer the smaller regular correction when it already reaches the boundary.
    if (direction == "sell" and 0 < liquidation <= ceiling
            and (regular == 0 or regular * item.price < needed)):
        return liquidation
    return max(Decimal(0), regular)
