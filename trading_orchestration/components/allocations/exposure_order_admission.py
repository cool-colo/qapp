from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor


class ExposureOrderAdmission:
    """Recheck every physical child order against actual and pending exposure."""

    def __init__(self, tolerance=Decimal("0.05")):
        self.assessor = ExposureAssessor(tolerance)

    def quantity(self, context, instrument_id, direction, requested):
        assessment = self.assessor.assess(context)
        if assessment.status != "correction" or assessment.direction != direction:
            return Decimal(0)
        item = context.instruments.get(instrument_id)
        if (item is None or item.price <= 0 or item.increment <= 0
                or not (item.can_buy if direction == "buy" else item.can_sell)
                or context.pending_orders != len(context.pending)):
            return Decimal(0)
        pending_value, pending_quantity = Decimal(0), Decimal(0)
        for pending in context.pending:
            if pending.direction != direction:
                return Decimal(0)
            priced = context.instruments.get(pending.instrument_id)
            if priced is None or priced.price <= 0:
                return Decimal(0)
            pending_value += pending.quantity * priced.price
            if pending.instrument_id == instrument_id:
                pending_quantity += pending.quantity
        needed = max(Decimal(0), assessment.minimum_notional - pending_value)
        if needed == 0:
            return Decimal(0)
        room = max(Decimal(0), assessment.maximum_notional - pending_value)
        target = context.target.quantities.get(instrument_id, Decimal(0))
        remaining = (target - item.quantity if direction == "buy" else item.quantity - target) - pending_quantity
        required_qty = (needed / item.price / item.increment).to_integral_value(
            rounding=ROUND_CEILING) * item.increment
        room_qty = (room / item.price / item.increment).to_integral_value(
            rounding=ROUND_FLOOR) * item.increment
        allowed = min(requested, remaining, room_qty, max(item.minimum, required_qty))
        if direction == "sell":
            allowed = min(allowed, item.sellable)
        allowed = (allowed / item.increment).to_integral_value(rounding=ROUND_FLOOR) * item.increment
        if direction == "buy" and (item.buy_price is None or
                                    allowed * item.buy_price <= item.minimum_buy_notional):
            return Decimal(0)
        return allowed if allowed >= item.minimum else Decimal(0)

