from decimal import Decimal
from trading_orchestration.contexts.exposure_context import ExposureContext, ExposureAssessment


class ExposureAssessor:
    def __init__(self, tolerance: Decimal = Decimal("0.05")):
        if not tolerance.is_finite() or not 0 < tolerance < 1:
            raise ValueError("invalid exposure tolerance")
        self.tolerance = tolerance

    def assess(self, context: ExposureContext) -> ExposureAssessment:
        if context.quality_error:
            return ExposureAssessment("unknown", context.quality_error)
        target = context.target
        if target is None or target.trading_date != context.trading_date:
            return ExposureAssessment("unknown", "no_current_formal_target")
        assets = context.total_assets
        if not assets.is_finite() or assets <= target.buffer_amount:
            return ExposureAssessment("unknown", "invalid_asset_denominator")
        value = Decimal(0)
        for item in context.instruments.values():
            if not item.quantity.is_finite() or item.quantity < 0:
                return ExposureAssessment("unknown", "invalid_position")
            if item.quantity > 0:
                if not item.price.is_finite() or item.price <= 0:
                    return ExposureAssessment("unknown", "missing_position_price")
                value += item.quantity * item.price
        denominator = assets - target.buffer_amount
        actual = value / denominator
        delta = actual - target.ratio
        common = dict(target_ratio=target.ratio, actual_ratio=actual, deviation=delta,
                      denominator=denominator, market_value=value)
        if abs(delta) <= self.tolerance:
            status = "settling" if context.pending_orders else "satisfied"
            return ExposureAssessment(status, "within_tolerance", **common)
        direction = "sell" if delta > 0 else "buy"
        return ExposureAssessment("correction", "outside_tolerance", **common,
                                  direction=direction,
                                  minimum_notional=(abs(delta) - self.tolerance) * denominator,
                                  maximum_notional=(abs(delta) + self.tolerance) * denominator)

