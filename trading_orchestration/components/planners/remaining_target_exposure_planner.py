from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.configuration.exposure_config import ExposureConfig
from trading_orchestration.contexts.exposure_context import ExposureContext, ExposurePlan


class RemainingTargetExposurePlanner:
    """Allocate only the corrective side of the remaining formal target."""

    def __init__(self, config: ExposureConfig):
        self.config = config
        self.assessor = ExposureAssessor(config.tolerance)

    def plan(self, context: ExposureContext) -> ExposurePlan:
        assessment = self.assessor.assess(context)
        version = "" if context.target is None else context.target.version
        base = dict(parent_version=version, execution_revision=context.execution_revision,
                    direction=assessment.direction)
        if assessment.status == "unknown":
            return ExposurePlan("unknown", assessment.reason, **base)
        if context.pending_orders:
            return ExposurePlan("waiting", "pending_orders", **base)
        if assessment.status == "satisfied":
            return ExposurePlan("satisfied", "within_tolerance", **base)
        target = context.target
        side = assessment.direction
        required, maximum = assessment.minimum_notional, assessment.maximum_notional
        excluded = {}
        candidates = []
        for iid in sorted(set(target.quantities) | set(context.instruments)):
            item = context.instruments.get(iid)
            if item is None:
                excluded[iid] = "missing_instrument"
                continue
            desired = target.quantities.get(iid, Decimal(0))
            remaining = desired - item.quantity if side == "buy" else item.quantity - desired
            if remaining <= 0:
                continue
            if not (item.can_buy if side == "buy" else item.can_sell):
                excluded[iid] = item.block_reason or "direction_not_permitted"
                continue
            if item.price <= 0 or not item.price.is_finite():
                return ExposurePlan("unknown", "invalid_candidate_price", **base)
            price = item.buy_price if side == "buy" else item.sell_price
            if price is None or price <= 0 or not price.is_finite():
                return ExposurePlan("unknown", "missing_executable_price", **base)
            if item.increment <= 0 or item.minimum <= 0:
                return ExposurePlan("unknown", "invalid_quantity_rules", **base)
            cap = remaining if side == "buy" else min(remaining, item.sellable)
            cap = (cap / item.increment).to_integral_value(rounding=ROUND_FLOOR) * item.increment
            if cap < item.minimum:
                excluded[iid] = "no_legal_remaining_quantity"
                continue
            rank = target.ranks.get(iid)
            ordering = ((rank is None, rank or 0, iid) if side == "buy"
                        else (rank is not None, -(rank or 0), iid))
            candidates.append((ordering, iid, item, cap, price))
        candidates.sort(key=lambda x: x[0])
        capacity = sum((cap * item.price for _, _, item, cap, _ in candidates), Decimal(0))
        details = dict(required_notional=required, available_notional=capacity, excluded=excluded)
        if capacity < required:
            return ExposurePlan("replan", "remaining_target_capacity_insufficient", **base, **details)

        quantities = {iid: item.quantity for iid, item in context.instruments.items()}
        achieved, cash = Decimal(0), context.free_cash
        for _, iid, item, cap, order_price in candidates:
            need = max(Decimal(0), required - achieved)
            if need == 0:
                break
            quantity = (need / item.price / item.increment).to_integral_value(
                rounding=ROUND_CEILING) * item.increment
            quantity = min(cap, max(item.minimum, quantity))
            room = ((maximum - achieved) / item.price / item.increment).to_integral_value(
                rounding=ROUND_FLOOR) * item.increment
            quantity = min(quantity, room)
            if side == "buy":
                affordable = max(Decimal(0), min(
                    cash / (order_price * (1 + self.config.commission_rate)),
                    (cash - self.config.minimum_fee) / order_price))
                quantity = min(quantity, (affordable / item.increment).to_integral_value(
                    rounding=ROUND_FLOOR) * item.increment)
                if quantity * order_price <= item.minimum_buy_notional:
                    excluded[iid] = "minimum_buy_notional"
                    continue
            if quantity < item.minimum:
                excluded[iid] = "budget_or_tolerance_prevents_legal_order"
                continue
            quantities[iid] = item.quantity + quantity if side == "buy" else item.quantity - quantity
            achieved += quantity * item.price
            if side == "buy":
                notional = quantity * order_price
                cash -= notional + max(self.config.minimum_fee,
                                      notional * self.config.commission_rate)
        if achieved < required:
            return ExposurePlan("replan", "legal_cash_or_lot_capacity_insufficient",
                                **base, **details, notional=achieved)
        return ExposurePlan("planned", "single_side_remaining_targets", **base, **details,
                            quantities=quantities, notional=achieved)

