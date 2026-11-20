from decimal import Decimal
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.components.allocations.exposure_quantity import (
    corrective_quantity, liquidation_quantity,
)


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
        cap = min(requested, remaining)
        liquidation = Decimal(0)
        if direction == "sell":
            cap = min(cap, item.sellable)
            liquidation = liquidation_quantity(item, target, pending_quantity)
        return corrective_quantity(item, direction, needed=needed, room=room, cap=cap,
                                   liquidation=liquidation)

